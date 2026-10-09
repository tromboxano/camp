import asyncio, json, sqlite3
import calendar as calendario_python
import hashlib
from datetime import date
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from nicegui import ui
from shapely.geometry import Point, Polygon

db = sqlite3.connect('campo.db', check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript('''
CREATE TABLE IF NOT EXISTS lotes(
    id INTEGER PRIMARY KEY,
    nombre TEXT,
    coords TEXT
);
CREATE TABLE IF NOT EXISTS eventos(
    id INTEGER PRIMARY KEY,
    lote_id INTEGER,
    fecha TEXT,
    tipo TEXT,
    cultivo TEXT,
    proposito TEXT,
    obs TEXT
);
CREATE TABLE IF NOT EXISTS animales(
    id INTEGER PRIMARY KEY,
    caravana TEXT UNIQUE,
    categoria TEXT,
    lote_id INTEGER,
    obs TEXT
);
CREATE TABLE IF NOT EXISTS implementos(id INTEGER PRIMARY KEY, nombre TEXT, tipo TEXT,
    marca_modelo TEXT, anio INTEGER, serie TEXT, horas REAL DEFAULT 0,
    costo_compra REAL DEFAULT 0, obs TEXT);
CREATE TABLE IF NOT EXISTS servicios(id INTEGER PRIMARY KEY, implemento_id INTEGER,
    fecha TEXT, tipo TEXT, horas REAL, costo REAL, realizado_por TEXT,
    prox_fecha TEXT, prox_horas REAL, obs TEXT);
CREATE TABLE IF NOT EXISTS tareas_calendario(
    id INTEGER PRIMARY KEY,
    fecha TEXT NOT NULL,
    tipo TEXT NOT NULL,
    titulo TEXT NOT NULL,
    nota TEXT
);
CREATE TABLE IF NOT EXISTS usuarios(
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    nombre TEXT,
    rol TEXT NOT NULL DEFAULT 'usuario'
);
CREATE TABLE IF NOT EXISTS insumos(
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL,
    categoria TEXT NOT NULL,
    cantidad REAL NOT NULL DEFAULT 0,
    unidad TEXT NOT NULL,
    costo_unitario REAL NOT NULL DEFAULT 0,
    obs TEXT
);
CREATE TABLE IF NOT EXISTS finanzas(
    id INTEGER PRIMARY KEY,
    fecha TEXT NOT NULL,
    tipo TEXT NOT NULL,
    categoria TEXT,
    descripcion TEXT,
    monto REAL NOT NULL DEFAULT 0,
    obs TEXT,
    origen_tipo TEXT,
    origen_id INTEGER
);
''')
columnas_insumos = {columna['name'] for columna in db.execute('PRAGMA table_info(insumos)')}
if 'costo_unitario' not in columnas_insumos:
    db.execute('ALTER TABLE insumos ADD COLUMN costo_unitario REAL NOT NULL DEFAULT 0')
    db.commit()

columnas_implementos = {columna['name'] for columna in db.execute('PRAGMA table_info(implementos)')}
if 'costo_compra' not in columnas_implementos:
    db.execute('ALTER TABLE implementos ADD COLUMN costo_compra REAL NOT NULL DEFAULT 0')
    db.commit()

columnas_finanzas = {columna['name'] for columna in db.execute('PRAGMA table_info(finanzas)')}
if 'origen_tipo' not in columnas_finanzas:
    db.execute('ALTER TABLE finanzas ADD COLUMN origen_tipo TEXT')
if 'origen_id' not in columnas_finanzas:
    db.execute('ALTER TABLE finanzas ADD COLUMN origen_id INTEGER')
db.execute(
    "UPDATE finanzas SET origen_tipo='insumo' WHERE origen_tipo IS NULL "
    "AND (descripcion LIKE 'Compra de insumo:%' OR descripcion LIKE 'Reposición de insumo:%')"
)
db.execute(
    "UPDATE finanzas SET origen_tipo='implemento' WHERE origen_tipo IS NULL "
    "AND (descripcion LIKE 'Compra de implemento:%' OR descripcion LIKE 'Servicio para %')"
)
db.commit()

# Usuario administrativo por defecto para permitir iniciar sesión de inmediato
usuario_admin = db.execute('SELECT id FROM usuarios WHERE username = ?', ('admin',)).fetchone()
if usuario_admin is None:
    db.execute(
        'INSERT INTO usuarios(username, password_hash, nombre, rol) VALUES(?,?,?,?)',
        ('admin', hashlib.sha256('admin123'.encode()).hexdigest(), 'Administrador', 'admin')
    )
    db.commit()

TIPOS_IMPL = ['tractor', 'sembradora', 'cosechadora', 'pulverizadora', 'arado', 'rastra',
                'acoplado', 'tolva', 'otro']
TIPOS_SERV = ['cambio de aceite', 'engrase', 'filtros', 'reparación', 'revisión general', 'otro']

sel = {'lote': None}     # lote seleccionado
capas = []               # polígonos dibujados (para poder redibujar)

TIPOS = ['siembra', 'arada', 'cosecha', 'pastoreo', 'otro']
PROPOSITOS = ['grano', 'forraje', 'cobertura', 'pastoreo directo', 'otro']
CATEGORIAS = ['vaca', 'vaquillona', 'ternero', 'novillo', 'toro', 'otro']
MESES_ES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
            'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
sel_cal = {'mes': date.today().replace(day=1), 'fecha': date.today()}
current_user = {'id': None, 'username': None, 'nombre': None, 'rol': None}


def hash_password(password):
    return hashlib.sha256((password or '').encode()).hexdigest()


def autenticar_usuario(username, password):
    username = (username or '').strip()
    if not username or not password:
        return None
    usuario = db.execute(
        'SELECT * FROM usuarios WHERE username = ?',
        (username,)
    ).fetchone()
    if usuario is None:
        return None
    if usuario['password_hash'] != hash_password(password):
        return None
    return dict(usuario)


def registrar_movimiento_financiero(fecha_val, tipo, categoria, descripcion, monto, obs='',
                                    origen_tipo=None, origen_id=None):
    valor = float(monto or 0)
    if valor <= 0:
        return False
    db.execute(
        'INSERT INTO finanzas(fecha, tipo, categoria, descripcion, monto, obs, origen_tipo, origen_id) '
        'VALUES(?,?,?,?,?,?,?,?)',
        (
            fecha_val or date.today().isoformat(),
            tipo,
            (categoria or '').strip() or 'General',
            (descripcion or '').strip(),
            valor,
            (obs or '').strip(),
            origen_tipo,
            origen_id,
        )
    )
    db.commit()
    panel_finanzas.refresh()
    return True


def formatear_pesos(valor):
    return '$ ' + f'{valor:,.2f}'.translate(str.maketrans(',.', '.,'))


def hectareas_de_poligono(coords):
    if not coords:
        return 0.0
    poly = Polygon([(lng, lat) for lat, lng in coords])
    if poly.is_empty:
        return 0.0
    latitudes = [lat for lat, _ in coords]
    lat_prom = sum(latitudes) / len(latitudes)
    lat_rad = lat_prom * 3.141592653589793 / 180
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * abs(__import__('math').cos(lat_rad))
    area_m2 = poly.area * m_per_deg_lat * m_per_deg_lon
    return area_m2 / 10_000.0

# ---------- MAPA ----------
def dibujar_lotes():
    for c in capas:
        mapa.remove_layer(c)
    capas.clear()
    for l in db.execute('SELECT * FROM lotes'):
        color = 'yellow' if sel['lote'] == l['id'] else 'lime'
        capas.append(mapa.generic_layer(name='polygon',
                     args=[json.loads(l['coords']), {'color': color, 'weight': 2}]))

def al_click(e):
    p = Point(e.args['latlng']['lng'], e.args['latlng']['lat'])
    for l in db.execute('SELECT * FROM lotes'):
        poly = Polygon([(lng, lat) for lat, lng in json.loads(l['coords'])])
        if poly.contains(p):
            sel['lote'] = l['id']
            dibujar_lotes(); panel_lote.refresh()
            return

def al_dibujar(e):
    anillo = e.args['layer']['_latlngs'][0]
    coords = [[p['lat'], p['lng']] for p in anillo]
    with ui.dialog() as d, ui.card():
        nombre = ui.input('Nombre del lote')
        def guardar():
            db.execute('INSERT INTO lotes(nombre, coords) VALUES(?,?)',
                       (nombre.value, json.dumps(coords)))
            db.commit(); d.close(); dibujar_lotes(); listado_lotes.refresh()
        ui.button('Guardar', on_click=guardar)
    d.open()

@ui.refreshable
def listado_lotes():
    lotes = db.execute(
        'SELECT l.id, l.nombre, l.coords, e.id AS evento_id, e.fecha, e.cultivo, e.obs '
        'FROM lotes l LEFT JOIN eventos e ON e.id = '
        '(SELECT id FROM eventos WHERE lote_id=l.id ORDER BY fecha DESC, id DESC LIMIT 1) '
        'ORDER BY l.nombre').fetchall()
    rows = []
    total_hectareas = 0.0
    for lote in lotes:
        hectareas = hectareas_de_poligono(json.loads(lote['coords']))
        total_hectareas += hectareas
        rows.append({
            'id': lote['id'],
            'evento_id': lote['evento_id'],
            'lote': lote['nombre'],
            'hectareas': f'{hectareas:.2f}',
            'cultivo': lote['cultivo'] or '-',
            'fecha': lote['fecha'] or '-',
            'obs': lote['obs'] or '',
        })

    ui.label(f'Superficie total: {total_hectareas:.2f} ha').classes('text-h6')
    tabla = ui.table(rows=rows, row_key='id', selection='single', columns=[
        {'name': 'lote', 'label': 'Lote', 'field': 'lote'},
        {'name': 'hectareas', 'label': 'Hectáreas', 'field': 'hectareas'},
        {'name': 'cultivo', 'label': 'Cultivo actual', 'field': 'cultivo'},
        {'name': 'fecha', 'label': 'Fecha', 'field': 'fecha'},
        {'name': 'obs', 'label': 'Observaciones', 'field': 'obs'},
    ]).classes('w-full')

    def editar_observaciones():
        if not tabla.selected:
            ui.notify('Seleccioná un lote', type='warning')
            return
        fila = tabla.selected[0]
        if fila['evento_id'] is None:
            ui.notify('El lote no tiene un registro para editar', type='warning')
            return
        with ui.dialog() as dialog, ui.card():
            observaciones = ui.textarea('Observaciones', value=fila['obs']).classes('w-full')

            def guardar_observaciones():
                db.execute('UPDATE eventos SET obs=? WHERE id=?',
                           (observaciones.value, fila['evento_id']))
                db.commit()
                dialog.close()
                listado_lotes.refresh()
                if sel['lote'] == fila['id']:
                    panel_lote.refresh()

            with ui.row():
                ui.button('Cancelar', on_click=dialog.close)
                ui.button('Guardar', on_click=guardar_observaciones)
        dialog.open()

    ui.button('Editar observaciones', on_click=editar_observaciones, icon='edit')

# ---------- PANEL DEL LOTE ----------
@ui.refreshable
def panel_lote():
    if sel['lote'] is None:
        ui.label('Hacé click en un lote del mapa'); return
    l = db.execute('SELECT * FROM lotes WHERE id=?', (sel['lote'],)).fetchone()
    coords = json.loads(l['coords'])
    hectareas = hectareas_de_poligono(coords)
    ui.label(f"Lote: {l['nombre']}").classes('text-h6')
    ui.label(f"Superficie: {hectareas:.2f} ha").classes('text-subtitle2')

    def eliminar_lote():
        with ui.dialog() as dialog, ui.card():
            ui.label(f"¿Seguro que querés eliminar el lote '{l['nombre']}'?")
            ui.label('Se borrarán también sus eventos asociados.').classes('text-grey')

            def confirmar():
                db.execute('DELETE FROM eventos WHERE lote_id=?', (l['id'],))
                db.execute('UPDATE animales SET lote_id=NULL WHERE lote_id=?', (l['id'],))
                db.execute('DELETE FROM lotes WHERE id=?', (l['id'],))
                db.commit()
                dialog.close()
                sel['lote'] = None
                dibujar_lotes()
                panel_lote.refresh()
                listado_lotes.refresh()

            with ui.row():
                ui.button('Cancelar', on_click=dialog.close)
                ui.button('Eliminar', on_click=confirmar).classes('bg-red-500 text-white')
        dialog.open()

    tipo = ui.select(TIPOS, label='Acción', value='siembra')
    fecha = ui.input('Fecha', value=date.today().isoformat()).props('type=date')
    cultivo = ui.input('Cultivo / pasto')
    prop = ui.select(PROPOSITOS, label='Propósito')
    obs = ui.textarea('Observaciones')

    def guardar():
        db.execute('INSERT INTO eventos(lote_id,fecha,tipo,cultivo,proposito,obs) '
                   'VALUES(?,?,?,?,?,?)',
                   (l['id'], fecha.value, tipo.value, cultivo.value, prop.value, obs.value))
        db.commit(); panel_lote.refresh(); listado_lotes.refresh()
    with ui.row():
        ui.button('Registrar', on_click=guardar)
        ui.button('Eliminar lote', on_click=eliminar_lote).classes('bg-red-500 text-white')

    rows = [dict(r) for r in db.execute(
        'SELECT id,fecha,tipo,cultivo,proposito,obs FROM eventos WHERE lote_id=? '
        'ORDER BY fecha DESC, id DESC', (l['id'],))]
    tabla_eventos = ui.table(rows=rows, row_key='id', selection='single',
        columns=[{'name': k, 'label': k, 'field': k}
                 for k in ['fecha', 'tipo', 'cultivo', 'proposito', 'obs']]).classes('w-full')

    def eliminar_evento():
        if not tabla_eventos.selected:
            ui.notify('Seleccioná el registro que querés eliminar', type='warning')
            return
        evento = tabla_eventos.selected[0]
        with ui.dialog() as dialog, ui.card():
            ui.label(f"¿Eliminar el registro del {evento['fecha']} ({evento['tipo']})?")
            with ui.row():
                ui.button('Cancelar', on_click=dialog.close)

                def confirmar():
                    db.execute('DELETE FROM eventos WHERE id=? AND lote_id=?',
                               (evento['id'], l['id']))
                    db.commit()
                    dialog.close()
                    panel_lote.refresh()
                    listado_lotes.refresh()

                ui.button('Eliminar', on_click=confirmar).classes('bg-red-500 text-white')
        dialog.open()

    ui.button('Eliminar registro seleccionado', on_click=eliminar_evento).classes(
        'bg-red-500 text-white')

# ---------- ANIMALES ----------
sel_animal = {'id': None, 'edit_id': None}

def opciones_animales():
    return {r['id']: f"{r['caravana']} · {r['categoria']}"
            for r in db.execute('SELECT id,caravana,categoria FROM animales ORDER BY caravana')}


def resumen_animales_stats():
    stats = [dict(r) for r in db.execute(
        'SELECT categoria, COUNT(*) AS total FROM animales GROUP BY categoria ORDER BY categoria')]
    total = sum(int(r['total']) for r in stats)
    if not stats:
        return [], total
    for r in stats:
        r['categoria'] = r['categoria'] or 'otro'
    return stats, total

@ui.refreshable
def resumen_animales():
    stats, total = resumen_animales_stats()
    with ui.card().classes('w-full'):
        ui.label('Resumen de animales').classes('text-h6')
        ui.label(f'Total de animales: {total}').classes('text-subtitle1')
        if total == 0:
            ui.label('Todavía no hay animales registrados').classes('text-grey')
            return

        colors = ['#4caf50', '#2196f3', '#ff9800', '#9c27b0', '#f44336', '#607d8b', '#00bcd4', '#8bc34a']
        gradient_parts = []
        acum = 0
        for idx, r in enumerate(stats):
            porcentaje = int(r['total']) / total * 100
            color = colors[idx % len(colors)]
            start = acum
            end = acum + porcentaje
            gradient_parts.append(f'{color} {start:.1f}% {end:.1f}%')
            acum = end
        conic = 'conic-gradient(' + ', '.join(gradient_parts) + ')'

        legend_html = ''.join(
            f'''
            <div style="display:flex; align-items:center; justify-content:space-between; gap:12px; margin: 7px 0; font-size: 14px;">
                <div style="display:flex; align-items:center; gap:8px;">
                    <span style="width:12px; height:12px; border-radius:3px; background:{colors[i % len(colors)]}; display:inline-block;"></span>
                    <span style="font-weight:500;">{r['categoria']}</span>
                </div>
                <span style="color:#555;">{r['total']} ({(int(r['total']) / total * 100) if total else 0:.1f}%)</span>
            </div>
            '''
            for i, r in enumerate(stats)
        )

        ui.html(f'''
            <div style="display:flex; align-items:center; gap:18px; margin: 14px 0 10px; flex-wrap: wrap;">
                <div style="width: 180px; height: 180px; border-radius: 50%; background: {conic};
                    position: relative; box-shadow: 0 6px 18px rgba(0,0,0,0.12), inset 0 0 0 1px rgba(0,0,0,0.06); flex-shrink: 0;">
                    <div style="position:absolute; inset: 22%; border-radius:50%; background: white; display:flex;
                        align-items:center; justify-content:center; font-weight:700; font-size: 1.2rem; color:#1f2937;
                        box-shadow: inset 0 0 0 1px rgba(0,0,0,0.05);">{total}</div>
                </div>
                <div style="flex: 1; min-width: 180px;">
                    {legend_html}
                </div>
            </div>
        ''')

@ui.refreshable
def tabla_animales():
    rows = [dict(r) for r in db.execute(
        'SELECT a.id, a.caravana, a.categoria, l.nombre AS lote, a.obs FROM animales a '
        'LEFT JOIN lotes l ON l.id=a.lote_id ORDER BY a.caravana')]
    ui.table(rows=rows, row_key='id', columns=[{'name': k, 'label': k, 'field': k}
             for k in ['caravana', 'categoria', 'lote', 'obs']]).classes('w-full')

@ui.refreshable
def detalle_animal():
    if sel_animal['id'] is None:
        ui.label('Elegí un animal para ver el detalle').classes('text-grey')
        return
    a = db.execute('SELECT a.*, l.nombre AS lote FROM animales a '
                   'LEFT JOIN lotes l ON l.id=a.lote_id WHERE a.id=?',
                   (sel_animal['id'],)).fetchone()
    if a is None:
        return
    with ui.card().classes('w-full'):
        ui.label(f"Caravana: {a['caravana']}").classes('text-subtitle1')
        ui.label(f"Categoría: {a['categoria'] or '-'}")
        ui.label(f"Lote actual: {a['lote'] or '-'}")
        ui.label(f"Obs: {a['obs'] or '-'}")

        def editar_actual():
            sel_animal['edit_id'] = sel_animal['id']
            form_animales.refresh()

        def eliminar_actual():
            with ui.dialog() as dialog, ui.card():
                ui.label(f"¿Seguro que querés eliminar a {a['caravana']}?")

                def confirmar():
                    db.execute('DELETE FROM animales WHERE id=?', (a['id'],))
                    db.commit()
                    dialog.close()
                    sel_animal['id'] = None
                    sel_animal['edit_id'] = None
                    refrescar_animales()
                    form_animales.refresh()

                with ui.row():
                    ui.button('Cancelar', on_click=dialog.close)
                    ui.button('Eliminar', on_click=confirmar).classes('bg-red-500 text-white')
            dialog.open()

        with ui.row():
            ui.button('Editar', on_click=editar_actual)
            ui.button('Eliminar', on_click=eliminar_actual).classes('bg-red-500 text-white')


def elegir_animal(e):
    sel_animal['id'] = e.value
    sel_animal['edit_id'] = None
    detalle_animal.refresh()


def refrescar_animales():
    if 'selector' in globals():
        selector.set_options(opciones_animales())
    tabla_animales.refresh()
    detalle_animal.refresh()
    resumen_animales.refresh()

@ui.refreshable
def form_animales():
    animal = None
    if sel_animal['edit_id'] is not None:
        animal = db.execute('SELECT * FROM animales WHERE id=?', (sel_animal['edit_id'],)).fetchone()

    titulo = 'Editar animal' if animal else 'Nuevo animal'
    with ui.card().classes('w-full'):
        ui.label(titulo).classes('text-h6')
        caravana = ui.input('Caravana', value=animal['caravana'] if animal else '')
        cat = ui.select(CATEGORIAS, label='Categoría', value=(animal['categoria'] if animal and animal['categoria'] else CATEGORIAS[0]))
        lotes = {r['id']: r['nombre'] for r in db.execute('SELECT id,nombre FROM lotes ORDER BY nombre')}
        lote = ui.select(lotes, label='Lote actual (opcional)',
                         value=(animal['lote_id'] if animal and animal['lote_id'] is not None else None),
                         clearable=True)
        obs = ui.textarea('Observaciones', value=animal['obs'] if animal else '')

        def guardar():
            nombre = (caravana.value or '').strip()
            if not nombre:
                ui.notify('Falta la caravana', type='warning'); return
            try:
                if animal is None:
                    db.execute('INSERT INTO animales(caravana,categoria,lote_id,obs) VALUES(?,?,?,?)',
                               (nombre, cat.value, lote.value, obs.value))
                    ui.notify('Animal agregado', type='positive')
                else:
                    db.execute('UPDATE animales SET caravana=?, categoria=?, lote_id=?, obs=? WHERE id=?',
                               (nombre, cat.value, lote.value, obs.value, animal['id']))
                    sel_animal['id'] = animal['id']
                    ui.notify('Animal actualizado', type='positive')
                db.commit()
            except sqlite3.IntegrityError:
                ui.notify('Esa caravana ya existe', type='negative'); return

            sel_animal['edit_id'] = None
            form_animales.refresh()
            refrescar_animales()

        def cancelar_edicion():
            sel_animal['edit_id'] = None
            form_animales.refresh()

        if animal is None:
            ui.button('Agregar', on_click=guardar)
        else:
            with ui.row():
                ui.button('Guardar cambios', on_click=guardar)
                ui.button('Cancelar', on_click=cancelar_edicion).classes('text-grey')

# ---------- IMPLEMENTOS ----------
sel_impl = {'id': None}

def opciones_impl():
    return {r['id']: r['nombre'] for r in db.execute('SELECT id,nombre FROM implementos ORDER BY nombre')}

def estado_servicio(imp):
    """Devuelve (último servicio, lista de motivos de vencimiento)."""
    ult = db.execute('SELECT * FROM servicios WHERE implemento_id=? '
                     'ORDER BY fecha DESC, id DESC LIMIT 1', (imp['id'],)).fetchone()
    if ult is None:
        return None, []
    motivos = []
    if ult['prox_fecha'] and ult['prox_fecha'] < date.today().isoformat():
        motivos.append('fecha vencida')
    if ult['prox_horas'] and (imp['horas'] or 0) >= ult['prox_horas']:
        motivos.append('horas alcanzadas')
    return ult, motivos

@ui.refreshable
def vencimientos():
    filas = []
    for imp in db.execute('SELECT * FROM implementos'):
        ult, motivos = estado_servicio(imp)
        if motivos:
            filas.append({'implemento': imp['nombre'], 'motivo': ', '.join(motivos),
                          'prox_fecha': ult['prox_fecha'] or '-',
                          'prox_horas': ult['prox_horas'] or '-', 'horas': imp['horas']})
    if not filas:
        ui.label('Sin servicios vencidos').classes('text-positive'); return
    ui.table(rows=filas, columns=[{'name': k, 'label': k, 'field': k}
             for k in ['implemento', 'motivo', 'prox_fecha', 'prox_horas', 'horas']]).classes('w-full')

@ui.refreshable
def form_impl():
    with ui.expansion('Nuevo implemento', icon='add').classes('w-full'):
        nombre = ui.input('Nombre (ej: Tractor JD 5705)')
        marca = ui.input('Marca / modelo')
        anio = ui.number('Año', format='%.0f')
        serie = ui.input('N° de serie / chasis')
        horas = ui.number('Horas actuales (horómetro)', value=0)
        costo_compra = ui.number('Costo de compra ($)', value=0).props('min=0 step=0.01')
        obs = ui.textarea('Observaciones')
        def guardar():
            if not nombre.value:
                ui.notify('Falta el nombre', type='warning'); return
            costo = float(costo_compra.value or 0)
            cursor = db.execute(
                'INSERT INTO implementos(nombre,tipo,marca_modelo,anio,serie,horas,costo_compra,obs) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (nombre.value, '', marca.value,
                 int(anio.value) if anio.value else None,
                 serie.value, horas.value or 0, costo, obs.value)
            )
            if costo > 0:
                registrar_movimiento_financiero(
                    date.today().isoformat(),
                    'Egreso',
                    'Implementos',
                    f"Compra de implemento: {nombre.value}",
                    costo,
                    obs.value or f"Compra de {nombre.value}",
                    origen_tipo='implemento',
                    origen_id=cursor.lastrowid
                )
            db.commit()
            form_impl.refresh()
            selector_impl.set_options(opciones_impl())
            vencimientos.refresh()
        ui.button('Guardar implemento', on_click=guardar)

@ui.refreshable
def panel_impl():
    if sel_impl['id'] is None:
        ui.label('Elegí un implemento').classes('text-grey'); return
    imp = db.execute('SELECT * FROM implementos WHERE id=?', (sel_impl['id'],)).fetchone()
    if imp is None:
        return
    ult, motivos = estado_servicio(imp)

    with ui.card().classes('w-full'):
        ui.label(f"{imp['nombre']} ({imp['tipo']})").classes('text-h6')
        ui.label(f"{imp['marca_modelo'] or '-'} · año {imp['anio'] or '-'} · serie {imp['serie'] or '-'}")
        ui.label(f"Horas actuales: {imp['horas'] or 0}")
        if ult is None:
            ui.label('Sin servicios registrados').classes('text-grey')
        else:
            ui.label(f"Último servicio: {ult['fecha']} ({ult['tipo']})")
            color = 'text-negative' if motivos else 'text-positive'
            ui.label(f"Próximo: {ult['prox_fecha'] or '-'} / {ult['prox_horas'] or '-'} hs"
                     + (f"  ⚠ {', '.join(motivos)}" if motivos else '')).classes(color)

    ui.label('Registrar servicio').classes('text-subtitle1')
    fecha = ui.input('Fecha del servicio', value=date.today().isoformat()).props('type=date')
    tipo = ui.select(TIPOS_SERV, label='Tipo de servicio', value=TIPOS_SERV[0])
    horas = ui.number('Horas del implemento al momento del servicio')
    costo = ui.number('Costo (opcional)')
    quien = ui.input('Realizado por')
    pf = ui.input('Próximo servicio (fecha)').props('type=date')
    ph = ui.number('Próximo servicio (a las X horas)')
    obs = ui.textarea('Observaciones')

    def guardar():
        costo_servicio = float(costo.value or 0)
        cursor = db.execute(
            'INSERT INTO servicios(implemento_id,fecha,tipo,horas,costo,realizado_por,'
            'prox_fecha,prox_horas,obs) VALUES(?,?,?,?,?,?,?,?,?)',
            (imp['id'], fecha.value, tipo.value, horas.value, costo.value, quien.value,
             pf.value, ph.value, obs.value)
        )
        if costo_servicio > 0:
            registrar_movimiento_financiero(
                fecha.value or date.today().isoformat(),
                'Egreso',
                imp['tipo'] or 'Implementos',
                f"Servicio para {imp['nombre']}: {tipo.value}",
                costo_servicio,
                obs.value or f"Servicio {tipo.value} para {imp['nombre']}",
                origen_tipo='servicio',
                origen_id=cursor.lastrowid
            )
        if horas.value and horas.value > (imp['horas'] or 0):
            db.execute('UPDATE implementos SET horas=? WHERE id=?', (horas.value, imp['id']))
        db.commit()
        panel_impl.refresh(); vencimientos.refresh()
    ui.button('Registrar servicio', on_click=guardar)

    rows = [dict(r) for r in db.execute(
        'SELECT fecha,tipo,horas,costo,realizado_por,prox_fecha,prox_horas,obs '
        'FROM servicios WHERE implemento_id=? ORDER BY fecha DESC, id DESC', (imp['id'],))]
    ui.table(rows=rows, columns=[{'name': k, 'label': k, 'field': k} for k in
             ['fecha', 'tipo', 'horas', 'costo', 'realizado_por', 'prox_fecha', 'prox_horas', 'obs']]
             ).classes('w-full')

def elegir_impl(e):
    sel_impl['id'] = e.value
    panel_impl.refresh()

# ---------- INSUMOS ----------
TIPOS_INSUMOS = ['Semillas', 'Agroquímicos', 'Fertilizantes', 'Alambres y cercos',
                 'Combustibles', 'Repuestos', 'Alimentos', 'Otros']
UNIDADES_INSUMOS = ['bolsas', 'kg', 'g', 'L', 'ml', 'bidones', 'rollos', 'm',
                    'unidades', 'toneladas']
sel_insumo = {'id': None, 'edit_id': None}

def formatear_pesos(valor):
    return '$ ' + f'{valor:,.2f}'.translate(str.maketrans(',.', '.,'))

def opciones_insumos():
    return {r['id']: r['nombre'] for r in db.execute(
        'SELECT id,nombre FROM insumos ORDER BY nombre')}

def elegir_insumo(e):
    sel_insumo['id'] = e.value
    detalle_insumo.refresh()

def refrescar_insumos():
    if 'selector_insumos' in globals():
        selector_insumos.set_options(opciones_insumos())
        selector_insumos.set_value(sel_insumo['id'])
    tabla_insumos.refresh()
    detalle_insumo.refresh()

@ui.refreshable
def form_insumos():
    insumo = None
    if sel_insumo['edit_id'] is not None:
        insumo = db.execute('SELECT * FROM insumos WHERE id=?',
                            (sel_insumo['edit_id'],)).fetchone()

    with ui.card().classes('w-full'):
        ui.label('Editar insumo' if insumo else 'Nuevo insumo').classes('text-h6')
        nombre = ui.input('Producto o suministro',
                          value=insumo['nombre'] if insumo else '')
        categoria = ui.select(TIPOS_INSUMOS, label='Tipo',
                              value=insumo['categoria'] if insumo else TIPOS_INSUMOS[0])
        cantidad = ui.number('Cantidad en stock',
                              value=insumo['cantidad'] if insumo else 0).props('min=0 step=any')
        costo_unitario = ui.number('Costo unitario ($)',
                              value=insumo['costo_unitario'] if insumo else 0) \
            .props('min=0 step=0.01')
        unidad = ui.select(UNIDADES_INSUMOS, label='Unidad',
                           value=insumo['unidad'] if insumo else UNIDADES_INSUMOS[0])
        obs = ui.textarea('Observaciones', value=insumo['obs'] if insumo else '')

        def guardar():
            nombre_valor = (nombre.value or '').strip()
            if not nombre_valor:
                ui.notify('Ingresá el nombre del producto', type='warning')
                return
            if cantidad.value is None or cantidad.value < 0:
                ui.notify('La cantidad no puede ser negativa', type='warning')
                return
            if costo_unitario.value is None or costo_unitario.value < 0:
                ui.notify('El costo unitario no puede ser negativo', type='warning')
                return
            costo_total = float(cantidad.value or 0) * float(costo_unitario.value or 0)
            if insumo is None:
                cursor = db.execute(
                    'INSERT INTO insumos(nombre,categoria,cantidad,unidad,costo_unitario,obs) '
                    'VALUES(?,?,?,?,?,?)',
                    (nombre_valor, categoria.value, cantidad.value, unidad.value,
                     costo_unitario.value, obs.value))
                if costo_total > 0:
                    registrar_movimiento_financiero(
                        date.today().isoformat(),
                        'Egreso',
                        categoria.value or 'Insumos',
                        f"Compra de insumo: {nombre_valor}",
                        costo_total,
                        obs.value or f"Stock inicial de {nombre_valor}",
                        origen_tipo='insumo',
                        origen_id=cursor.lastrowid
                    )
                sel_insumo['id'] = cursor.lastrowid
                ui.notify('Insumo agregado', type='positive')
            else:
                costo_actual = float((insumo['cantidad'] or 0) * (insumo['costo_unitario'] or 0))
                delta_costo = costo_total - costo_actual
                db.execute('UPDATE insumos SET nombre=?, categoria=?, cantidad=?, unidad=?, '
                           'costo_unitario=?, obs=? WHERE id=?',
                           (nombre_valor, categoria.value, cantidad.value, unidad.value,
                            costo_unitario.value, obs.value, insumo['id']))
                if delta_costo > 0:
                    registrar_movimiento_financiero(
                        date.today().isoformat(),
                        'Egreso',
                        categoria.value or 'Insumos',
                        f"Compra de insumo: {nombre_valor}",
                        delta_costo,
                        f"Ajuste de stock para {nombre_valor}",
                        origen_tipo='insumo',
                        origen_id=insumo['id']
                    )
                sel_insumo['id'] = insumo['id']
                ui.notify('Insumo actualizado', type='positive')
            db.commit()
            sel_insumo['edit_id'] = None
            form_insumos.refresh()
            refrescar_insumos()

        if insumo is None:
            ui.button('Agregar insumo', on_click=guardar, icon='add').classes('w-full')
        else:
            with ui.row():
                ui.button('Guardar cambios', on_click=guardar, icon='save')

                def cancelar_edicion():
                    sel_insumo['edit_id'] = None
                    form_insumos.refresh()

                ui.button('Cancelar', on_click=cancelar_edicion).classes('text-grey')

@ui.refreshable
def tabla_insumos():
    rows = [dict(r) for r in db.execute(
        'SELECT nombre,categoria,cantidad,unidad,costo_unitario,obs FROM insumos '
        'ORDER BY categoria,nombre')]
    if not rows:
        ui.label('Todavía no hay insumos registrados').classes('text-grey')
        return
    valor_stock = sum(row['cantidad'] * row['costo_unitario'] for row in rows)
    ui.label(f'Valor estimado del stock: {formatear_pesos(valor_stock)}') \
        .classes('text-subtitle1')
    for row in rows:
        row['valor_total'] = formatear_pesos(row['cantidad'] * row['costo_unitario'])
        row['costo_unitario'] = formatear_pesos(row['costo_unitario'])
    ui.table(rows=rows, columns=[
        {'name': 'nombre', 'label': 'Producto', 'field': 'nombre'},
        {'name': 'categoria', 'label': 'Tipo', 'field': 'categoria'},
        {'name': 'cantidad', 'label': 'Cantidad', 'field': 'cantidad'},
        {'name': 'unidad', 'label': 'Unidad', 'field': 'unidad'},
        {'name': 'costo_unitario', 'label': 'Costo unitario', 'field': 'costo_unitario'},
        {'name': 'valor_total', 'label': 'Valor en stock', 'field': 'valor_total'},
        {'name': 'obs', 'label': 'Observaciones', 'field': 'obs'},
    ]).classes('w-full')

@ui.refreshable
def detalle_insumo():
    if sel_insumo['id'] is None:
        ui.label('Elegí un insumo para ajustar su stock').classes('text-grey')
        return
    insumo = db.execute('SELECT * FROM insumos WHERE id=?',
                         (sel_insumo['id'],)).fetchone()
    if insumo is None:
        sel_insumo['id'] = None
        ui.label('Elegí un insumo para ajustar su stock').classes('text-grey')
        return

    with ui.card().classes('w-full'):
        ui.label(insumo['nombre']).classes('text-h6')
        ui.label(f"{insumo['categoria']} · Stock: {insumo['cantidad']:g} {insumo['unidad']}")
        ui.label(f"Costo unitario: {formatear_pesos(insumo['costo_unitario'])} · "
                 f"Valor en stock: {formatear_pesos(insumo['cantidad'] * insumo['costo_unitario'])}")
        if insumo['obs']:
            ui.label(insumo['obs'])
        with ui.row():
            def editar_insumo():
                sel_insumo['edit_id'] = insumo['id']
                form_insumos.refresh()

            ui.button('Editar', on_click=editar_insumo, icon='edit')

            def eliminar_insumo():
                with ui.dialog() as dialog, ui.card():
                    ui.label(
                        f"¿Seguro que querés eliminar {insumo['nombre']} y sus movimientos financieros vinculados?"
                    )

                    def confirmar():
                        db.execute(
                            'DELETE FROM finanzas WHERE origen_tipo=? AND '
                            '(origen_id=? OR (origen_id IS NULL AND descripcion IN (?, ?)))',
                            (
                                'insumo',
                                insumo['id'],
                                f"Compra de insumo: {insumo['nombre']}",
                                f"Reposición de insumo: {insumo['nombre']}",
                            )
                        )
                        db.execute('DELETE FROM insumos WHERE id=?', (insumo['id'],))
                        db.commit()
                        sel_insumo['id'] = None
                        sel_insumo['edit_id'] = None
                        dialog.close()
                        form_insumos.refresh()
                        refrescar_insumos()
                        panel_finanzas.refresh()
                        ui.notify('Insumo y movimientos vinculados eliminados', type='positive')

                    with ui.row():
                        ui.button('Cancelar', on_click=dialog.close)
                        ui.button('Eliminar', on_click=confirmar).classes('bg-red-500 text-white')
                dialog.open()

            ui.button('Eliminar', on_click=eliminar_insumo, icon='delete') \
                .classes('bg-red-500 text-white')

        ui.separator()
        ui.label('Ajustar stock').classes('text-subtitle1')
        with ui.row().classes('w-full items-end'):
            tipo_ajuste = ui.select(['Entrada', 'Salida'], value='Entrada', label='Movimiento')
            cantidad_ajuste = ui.number('Cantidad').props('min=0.01 step=any')

            def ajustar_stock():
                valor = cantidad_ajuste.value
                if valor is None or valor <= 0:
                    ui.notify('Ingresá una cantidad mayor que cero', type='warning')
                    return
                nueva_cantidad = insumo['cantidad'] + valor
                if tipo_ajuste.value == 'Salida':
                    nueva_cantidad = insumo['cantidad'] - valor
                    if nueva_cantidad < 0:
                        ui.notify('No hay stock suficiente para esa salida', type='warning')
                        return
                db.execute('UPDATE insumos SET cantidad=? WHERE id=?',
                           (nueva_cantidad, insumo['id']))
                db.commit()
                if tipo_ajuste.value == 'Entrada' and insumo['costo_unitario'] > 0:
                    registrar_movimiento_financiero(
                        date.today().isoformat(),
                        'Egreso',
                        insumo['categoria'] or 'Insumos',
                        f"Reposición de insumo: {insumo['nombre']}",
                        valor * insumo['costo_unitario'],
                        f"Entrada de {valor:g} {insumo['unidad']}",
                        origen_tipo='insumo',
                        origen_id=insumo['id']
                    )
                cantidad_ajuste.set_value(None)
                ui.notify('Stock actualizado', type='positive')
                tabla_insumos.refresh()
                detalle_insumo.refresh()

            ui.button('Aplicar', on_click=ajustar_stock, icon='sync_alt')

def seleccionar_fecha_calendario(fecha):
    sel_cal['fecha'] = fecha
    fecha_nota.set_value(fecha.isoformat())
    panel_calendario.refresh()

def cambiar_mes_calendario(cambio):
    mes_actual = sel_cal['mes']
    indice_mes = mes_actual.year * 12 + mes_actual.month - 1 + cambio
    anio, mes_cero = divmod(indice_mes, 12)
    mes_nuevo = mes_cero + 1
    dia = min(sel_cal['fecha'].day,
              calendario_python.monthrange(anio, mes_nuevo)[1])
    sel_cal['mes'] = date(anio, mes_nuevo, 1)
    seleccionar_fecha_calendario(date(anio, mes_nuevo, dia))

def callback_seleccionar_fecha(fecha):
    def seleccionar():
        seleccionar_fecha_calendario(fecha)
    return seleccionar

def callback_eliminar_tarea(tarea_id):
    def eliminar():
        db.execute('DELETE FROM tareas_calendario WHERE id=?', (tarea_id,))
        db.commit()
        panel_calendario.refresh()
    return eliminar

@ui.refreshable
def panel_calendario():
    mes = sel_cal['mes']
    fecha_seleccionada = sel_cal['fecha']
    if mes.month == 12:
        proximo_mes = date(mes.year + 1, 1, 1)
    else:
        proximo_mes = date(mes.year, mes.month + 1, 1)
    tareas = db.execute(
        'SELECT id, fecha, tipo, titulo, nota FROM tareas_calendario '
        'WHERE fecha>=? AND fecha<? ORDER BY fecha, id',
        (mes.isoformat(), proximo_mes.isoformat())).fetchall()
    tareas_por_fecha = {}
    for tarea in tareas:
        tareas_por_fecha.setdefault(tarea['fecha'], []).append(tarea)

    with ui.row().classes('calendar-toolbar items-center justify-between w-full'):
        with ui.row().classes('items-center'):
            ui.button(icon='chevron_left', on_click=lambda: cambiar_mes_calendario(-1)) \
                .props('flat round dense').tooltip('Mes anterior')
            ui.button(icon='chevron_right', on_click=lambda: cambiar_mes_calendario(1)) \
                .props('flat round dense').tooltip('Mes siguiente')
        ui.label(f'{MESES_ES[mes.month - 1].capitalize()} {mes.year}') \
            .classes('text-h5 calendar-month-title')
        ui.button('Hoy', on_click=lambda: seleccionar_fecha_calendario(date.today())) \
            .props('outline dense')

    with ui.element('div').classes('calendar-grid'):
        for dia_semana in ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']:
            ui.label(dia_semana).classes('calendar-weekday')
        for semana in calendario_python.monthcalendar(mes.year, mes.month):
            for numero_dia in semana:
                if numero_dia == 0:
                    ui.element('div').classes('calendar-day empty')
                    continue
                fecha = date(mes.year, mes.month, numero_dia)
                fecha_iso = fecha.isoformat()
                clases = 'calendar-day'
                if fecha == fecha_seleccionada:
                    clases += ' selected'
                if fecha == date.today():
                    clases += ' today'
                with ui.element('div').classes(clases):
                    ui.button(str(numero_dia),
                              on_click=callback_seleccionar_fecha(fecha)) \
                        .props('flat dense').classes('calendar-day-button')
                    tareas_del_dia = tareas_por_fecha.get(fecha_iso, [])
                    for tarea in tareas_del_dia[:2]:
                        observacion = (tarea['nota'] or '').strip()
                        if observacion:
                            ui.label(observacion).classes('calendar-event-note')

    ui.label(f'Notas para el {fecha_seleccionada.strftime("%d/%m/%Y")}') \
        .classes('text-h6 calendar-agenda-title')
    tareas_del_dia = tareas_por_fecha.get(fecha_seleccionada.isoformat(), [])
    if not tareas_del_dia:
        ui.label('No hay tareas anotadas para este día').classes('text-grey')
    for tarea in tareas_del_dia:
        with ui.row().classes('calendar-task-row items-start justify-between w-full'):
            with ui.column().classes('gap-0'):
                ui.label(f"{tarea['tipo']} · {tarea['titulo']}").classes('font-medium')
                if tarea['nota']:
                    ui.label(tarea['nota']).classes('text-grey-8')
            ui.button(icon='delete', on_click=callback_eliminar_tarea(tarea['id'])) \
                .props('flat round dense').tooltip('Eliminar nota')

# ---------- UI ----------
ui.add_head_html('''
<style>
:root {
    --campo-fondo: #f2f4ed;
    --campo-superficie: #ffffff;
    --campo-tinta: #29352c;
    --campo-verde: #526d4b;
    --campo-verde-suave: #e8eee4;
    --campo-tierra: #a8784c;
    --campo-borde: #d9dfd4;
}

body {
    background: var(--campo-fondo);
    color: var(--campo-tinta);
    font-family: 'Trebuchet MS', 'Segoe UI', sans-serif;
}

.q-tab-panels,
.q-tab-panel {
    background: transparent;
}

.q-tab-panel {
    padding: 16px 20px 24px;
}

.q-tabs {
    max-width: 100%;
    min-width: 0;
    padding: 4px;
    margin-bottom: 8px;
    border: 1px solid var(--campo-borde);
    border-radius: 8px;
    background: var(--campo-verde-suave);
}

.q-tabs__content {
    min-width: 0;
    overflow-x: auto;
}

.q-tab {
    min-height: 40px;
    border-radius: 6px;
    color: #647061;
    transition: background-color 140ms ease, color 140ms ease;
}

.q-tab--active {
    color: var(--campo-verde);
    background: var(--campo-superficie);
    font-weight: 600;
}

.q-tab__indicator {
    background: var(--campo-verde);
}

.q-card,
.q-table__container {
    border: 1px solid var(--campo-borde);
    border-radius: 8px;
    box-shadow: 0 2px 8px rgba(41, 53, 44, 0.06);
}

.q-table thead tr {
    background: var(--campo-verde-suave);
    color: var(--campo-tinta);
}

.q-table tbody tr:hover {
    background: #f5f7f2;
}

.q-field--outlined .q-field__control:before {
    border-color: var(--campo-borde);
}

.q-btn {
    border-radius: 6px;
    font-weight: 600;
    letter-spacing: 0;
    text-transform: none;
}

.q-checkbox__inner--truthy .q-checkbox__bg {
    background: var(--campo-verde);
    border-color: var(--campo-verde);
}

.calendar-layout {
    display: grid !important;
    grid-template-columns: minmax(250px, 320px) minmax(0, 1fr);
    gap: 24px;
    align-items: start;
}

.calendar-sidebar {
    min-width: 0;
    padding-right: 20px;
    border-right: 1px solid var(--campo-borde);
}

.calendar-main {
    min-width: 0;
}

.calendar-toolbar {
    min-height: 52px;
    margin-bottom: 12px;
}

.calendar-month-title {
    color: var(--campo-tinta);
    font-size: 1.3rem;
    font-weight: 600;
}

.calendar-grid {
    display: grid;
    width: 100%;
    grid-template-columns: repeat(7, minmax(0, 1fr));
    overflow: hidden;
    border-top: 1px solid var(--campo-borde);
    border-left: 1px solid var(--campo-borde);
    border-radius: 8px;
    background: var(--campo-superficie);
}

.calendar-weekday,
.calendar-day {
    min-width: 0;
    border-right: 1px solid var(--campo-borde);
    border-bottom: 1px solid var(--campo-borde);
}

.calendar-weekday {
    padding: 9px 6px;
    background: var(--campo-verde-suave);
    color: #647061;
    font-size: 0.8rem;
    font-weight: 600;
    text-align: center;
}

.calendar-day {
    display: flex;
    min-height: 88px;
    flex-direction: column;
    align-items: flex-start;
    padding: 6px;
    background: var(--campo-superficie);
}

.calendar-day.empty {
    background: #f7f8f5;
}

.calendar-day.selected {
    background: var(--campo-verde-suave);
    box-shadow: inset 0 0 0 1px var(--campo-verde);
}

.calendar-day.today .calendar-day-button {
    color: var(--campo-tierra);
    font-weight: 700;
}

.calendar-day-button {
    min-width: 30px;
    min-height: 30px;
}

.calendar-event-count {
    overflow: hidden;
    max-width: 100%;
    color: var(--campo-verde);
    font-size: 0.7rem;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.calendar-event-note {
    display: -webkit-box;
    overflow: hidden;
    max-width: 100%;
    color: #52604f;
    font-size: 0.68rem;
    line-height: 1.2;
    overflow-wrap: anywhere;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
}

.calendar-event-more {
    color: var(--campo-tierra);
    font-size: 0.65rem;
    font-weight: 600;
}

.calendar-agenda-title {
    margin-top: 20px;
}

.calendar-task-row {
    padding: 10px 0;
    border-bottom: 1px solid var(--campo-borde);
}

@media (max-width: 700px) {
    .q-tab-panel {
        padding: 12px 8px 18px;
    }

    .q-tab {
        padding: 0 8px;
        font-size: 0.8rem;
    }

    .calendar-layout {
        grid-template-columns: minmax(0, 1fr);
        gap: 18px;
    }

    .calendar-sidebar {
        padding: 0 0 16px;
        border-right: 0;
        border-bottom: 1px solid var(--campo-borde);
    }

    .calendar-day {
        min-height: 62px;
        padding: 3px;
    }

    .calendar-weekday {
        padding: 7px 2px;
        font-size: 0.7rem;
    }

    .calendar-event-count {
        font-size: 0.6rem;
    }
}
</style>
''')
ui.colors(primary='#526d4b', secondary='#a8784c', accent='#77845a')

usuario_label = ui.label('Sesión no iniciada')


def es_admin():
    return (current_user or {}).get('rol') == 'admin'


def abrir_login():
    usuario_login.value = ''
    clave_login.value = ''
    error_login.text = ''
    login_dialog.open()


def cerrar_sesion():
    global current_user
    current_user = {'id': None, 'username': None, 'nombre': None, 'rol': None}
    usuario_label.text = 'Sesión no iniciada'
    logout_btn.visible = False
    usuarios_admin_btn.visible = False
    abrir_login()


def abrir_panel_usuarios():
    if not es_admin():
        ui.notify('Solo el administrador puede ver esta sección.', type='negative')
        return

    with ui.dialog() as dialog, ui.card().classes('w-120'):
        ui.label('Usuarios del sistema').classes('text-h6')
        rows = [dict(r) for r in db.execute(
            'SELECT id, username, nombre, rol FROM usuarios ORDER BY rol, username'
        )]
        ui.table(
            rows=rows,
            row_key='id',
            columns=[
                {'name': 'username', 'label': 'Usuario', 'field': 'username'},
                {'name': 'nombre', 'label': 'Nombre', 'field': 'nombre'},
                {'name': 'rol', 'label': 'Rol', 'field': 'rol'},
            ],
        ).classes('w-full')
        ui.button('Cerrar', on_click=dialog.close).classes('w-full')
    dialog.open()


logout_btn = ui.button('Cerrar sesión', on_click=cerrar_sesion)
logout_btn.visible = False
usuarios_admin_btn = ui.button('Usuarios', icon='group', on_click=abrir_panel_usuarios)
usuarios_admin_btn.visible = False

with ui.row().classes('w-full justify-between items-center gap-3 q-pa-sm').style('position: sticky; top: 0; z-index: 20; width: 100%; margin-left: 0; margin-right: 0;'):
    usuario_label.classes('text-subtitle2 text-left')
    with ui.row().classes('items-center gap-2 ml-auto'):
        usuarios_admin_btn.classes('bg-grey-2 text-primary')
        logout_btn.classes('text-grey-9 bg-green-9 text-white')

ui.element('div').style('height: 8px;')

with ui.dialog() as login_dialog, ui.card().classes('w-80'):
    login_dialog.props('persistent')
    ui.label('Iniciar sesión').classes('text-h6')
    usuario_login = ui.input('Usuario').classes('w-full')
    clave_login = ui.input('Contraseña', password=True, password_toggle_button=True).classes('w-full')
    error_login = ui.label('').classes('text-negative')

    def iniciar_sesion():
        global current_user
        user = autenticar_usuario(usuario_login.value, clave_login.value)
        if user is None:
            error_login.text = 'Usuario o contraseña incorrectos'
            return
        current_user = {
            'id': user['id'],
            'username': user['username'],
            'nombre': user['nombre'],
            'rol': user['rol'],
        }
        usuario_label.text = f"{user['nombre'] or user['username']} · {user['rol']}"
        logout_btn.visible = True
        usuarios_admin_btn.visible = es_admin()
        rol_registro.enabled = es_admin()
        error_login.text = ''
        login_dialog.close()
        ui.notify(f"Bienvenido/a, {user['nombre'] or user['username']}")

    with ui.row().classes('w-full items-center'): 
        ui.button('Crear cuenta', on_click=lambda: (login_dialog.close(), registro_dialog.open())).classes('flex-1')
        ui.button('Entrar', on_click=iniciar_sesion, icon='login').classes('flex-1')

with ui.dialog() as registro_dialog, ui.card().classes('w-90'):
    registro_dialog.props('persistent')
    ui.label('Crear usuario').classes('text-h6')
    nombre_registro = ui.input('Nombre completo').classes('w-full')
    usuario_registro = ui.input('Nombre de usuario').classes('w-full')
    password_registro = ui.input('Contraseña', password=True, password_toggle_button=True).classes('w-full')
    confirmar_password = ui.input('Repetir contraseña', password=True, password_toggle_button=True).classes('w-full')
    rol_registro = ui.select(['usuario', 'admin'], value='usuario', label='Rol').classes('w-full')
    rol_registro.enabled = es_admin()
    error_registro = ui.label('').classes('text-negative')

    def abrir_login_desde_registro():
        registro_dialog.close()
        usuario_login.value = ''
        clave_login.value = ''
        error_login.text = ''
        login_dialog.open()

    def registrar_usuario():
        nombre = (nombre_registro.value or '').strip()
        username = (usuario_registro.value or '').strip()
        password = password_registro.value or ''
        confirmar = confirmar_password.value or ''

        if not nombre or not username:
            error_registro.text = 'Completá nombre y usuario'
            return
        if len(password) < 4:
            error_registro.text = 'La contraseña debe tener al menos 4 caracteres'
            return
        if password != confirmar:
            error_registro.text = 'Las contraseñas no coinciden'
            return
        if not es_admin() and rol_registro.value == 'admin':
            error_registro.text = 'Solo el administrador puede crear usuarios administradores.'
            return
        if db.execute('SELECT 1 FROM usuarios WHERE username = ?', (username,)).fetchone():
            error_registro.text = 'Ese nombre de usuario ya existe'
            return

        db.execute(
            'INSERT INTO usuarios(username, password_hash, nombre, rol) VALUES(?,?,?,?)',
            (username, hashlib.sha256(password.encode()).hexdigest(), nombre, 'admin' if es_admin() and rol_registro.value == 'admin' else 'usuario')
        )
        db.commit()

        error_registro.text = ''
        nombre_registro.value = ''
        usuario_registro.value = ''
        password_registro.value = ''
        confirmar_password.value = ''
        registro_dialog.close()
        usuario_login.value = username
        clave_login.value = ''
        error_login.text = 'Usuario creado. Iniciá sesión con tu usuario y contraseña.'
        login_dialog.open()
        ui.notify('Cuenta creada con éxito', type='positive')

    with ui.row().classes('w-full items-center'):
        ui.button('Volver', on_click=abrir_login_desde_registro).classes('flex-1')
        ui.button('Crear cuenta', on_click=registrar_usuario, icon='person_add').classes('flex-1')

login_dialog.open()

@ui.refreshable
def panel_finanzas():
    movimientos = [dict(r) for r in db.execute(
        'SELECT id, fecha, tipo, categoria, descripcion, monto, obs, origen_tipo, origen_id '
        'FROM finanzas ORDER BY fecha DESC, id DESC'
    )]

    saldo_ingresos = sum(float(m['monto']) for m in movimientos if m['tipo'] == 'Ingreso')
    saldo_egresos = sum(float(m['monto']) for m in movimientos if m['tipo'] in ('Egreso', 'Gasto'))
    saldo_total = saldo_ingresos - saldo_egresos

    with ui.card().classes('w-full'):
        ui.label('Finanzas').classes('text-h5')
        with ui.row().classes('w-full items-end q-col-gutter-md'):
            fecha = ui.input('Fecha', value=date.today().isoformat()).props('type=date')
            tipo = ui.select(['Ingreso', 'Egreso', 'Gasto'], value='Gasto', label='Tipo').classes('w-40')
            categoria = ui.input('Categoría', value='').classes('w-40')
            descripcion = ui.input('Descripción', value='').classes('w-60')
            monto = ui.number('Monto', value=0).props('min=0 step=0.01').classes('w-32')
            obs = ui.input('Observación', value='').classes('w-80')

            def guardar_movimiento():
                valor = monto.value
                if valor is None or valor <= 0:
                    ui.notify('Ingresá un monto válido.', type='negative')
                    return
                db.execute(
                    'INSERT INTO finanzas(fecha, tipo, categoria, descripcion, monto, obs) VALUES(?,?,?,?,?,?)',
                    (
                        fecha.value or date.today().isoformat(),
                        tipo.value,
                        (categoria.value or '').strip(),
                        (descripcion.value or '').strip(),
                        float(valor),
                        (obs.value or '').strip(),
                    )
                )
                db.commit()
                panel_finanzas.refresh()
                ui.notify('Movimiento guardado', type='positive')

            ui.button('Guardar', on_click=guardar_movimiento, icon='save').classes('w-32')

        with ui.row().classes('w-full q-col-gutter-md'):
            with ui.column().classes('w-1/3'):
                ui.card().classes('w-full').style('background:#eaf7ec; border:1px solid #cfe7d5;')
                with ui.card().classes('w-full').style('background:#eaf7ec; border:1px solid #cfe7d5;'):
                    ui.label('Ingresos').classes('text-subtitle2')
                    ui.label(formatear_pesos(saldo_ingresos)).classes('text-h6 text-positive')
            with ui.column().classes('w-1/3'):
                with ui.card().classes('w-full').style('background:#fff3f3; border:1px solid #f1d2d0;'):
                    ui.label('Egresos/Gastos').classes('text-subtitle2')
                    ui.label(formatear_pesos(saldo_egresos)).classes('text-h6 text-negative')
            with ui.column().classes('w-1/3'):
                with ui.card().classes('w-full').style('background:#edf4ff; border:1px solid #cfe0ff;'):
                    ui.label('Saldo').classes('text-subtitle2')
                    ui.label(formatear_pesos(saldo_total)).classes('text-h6')

        ui.label('Historial').classes('text-subtitle1 q-mt-md')
        if not movimientos:
            ui.label('Todavía no hay movimientos cargados.').classes('text-grey')
            return

        def editar_movimiento(movimiento):
            with ui.dialog() as dialog, ui.card().classes('w-full max-w-2xl'):
                ui.label('Editar movimiento').classes('text-h6')
                edit_fecha = ui.input('Fecha', value=movimiento['fecha']).props('type=date')
                edit_tipo = ui.select(
                    ['Ingreso', 'Egreso', 'Gasto'],
                    value=movimiento['tipo'],
                    label='Tipo'
                ).classes('w-full')
                edit_categoria = ui.input('Categoría', value=movimiento['categoria'] or '')
                edit_descripcion = ui.input('Descripción', value=movimiento['descripcion'] or '')
                edit_monto = ui.number(
                    'Monto', value=float(movimiento['monto'])
                ).props('min=0.01 step=0.01')
                edit_obs = ui.input('Observación', value=movimiento['obs'] or '')

                def guardar_cambios():
                    valor = edit_monto.value
                    if valor is None or valor <= 0:
                        ui.notify('Ingresá un monto válido.', type='negative')
                        return
                    if not edit_fecha.value:
                        ui.notify('Seleccioná una fecha válida.', type='negative')
                        return
                    db.execute(
                        'UPDATE finanzas SET fecha=?, tipo=?, categoria=?, descripcion=?, monto=?, obs=? '
                        'WHERE id=?',
                        (
                            edit_fecha.value,
                            edit_tipo.value,
                            (edit_categoria.value or '').strip(),
                            (edit_descripcion.value or '').strip(),
                            float(valor),
                            (edit_obs.value or '').strip(),
                            movimiento['id'],
                        )
                    )
                    db.commit()
                    dialog.close()
                    panel_finanzas.refresh()
                    ui.notify('Movimiento actualizado', type='positive')

                with ui.row().classes('w-full justify-end'):
                    ui.button('Cancelar', on_click=dialog.close)
                    ui.button('Guardar cambios', on_click=guardar_cambios, icon='save')
            dialog.open()

        def borrar_movimiento(movimiento):
            if movimiento['origen_tipo']:
                ui.notify(
                    'Este movimiento está vinculado a Insumos o Implementos y no se puede borrar desde Finanzas.',
                    type='warning'
                )
                return
            with ui.dialog() as dialog, ui.card():
                ui.label('¿Querés borrar este movimiento?')
                ui.label(
                    f"{movimiento['fecha']} · {movimiento['descripcion'] or movimiento['tipo']} · "
                    f"{formatear_pesos(float(movimiento['monto']))}"
                )

                def confirmar_borrado():
                    cursor = db.execute(
                        'DELETE FROM finanzas WHERE id=? AND origen_tipo IS NULL',
                        (movimiento['id'],)
                    )
                    if cursor.rowcount == 0:
                        dialog.close()
                        ui.notify(
                            'Este movimiento está vinculado y no se puede borrar desde Finanzas.',
                            type='warning'
                        )
                        return
                    db.commit()
                    dialog.close()
                    panel_finanzas.refresh()
                    ui.notify('Movimiento eliminado', type='positive')

                with ui.row().classes('w-full justify-end'):
                    ui.button('Cancelar', on_click=dialog.close)
                    ui.button('Borrar', on_click=confirmar_borrado, icon='delete') \
                        .classes('bg-red-600 text-white')
            dialog.open()

        with ui.row().classes('w-full items-center no-wrap text-weight-bold'):
            ui.label('Fecha').classes('w-24')
            ui.label('Tipo').classes('w-24')
            ui.label('Categoría').classes('w-32')
            ui.label('Descripción').classes('col')
            ui.label('Monto').classes('w-32 text-right')
            ui.label('Acciones').classes('w-28 text-center')
        ui.separator()
        for movimiento in movimientos:
            with ui.row().classes('w-full items-center no-wrap'):
                ui.label(movimiento['fecha']).classes('w-24')
                ui.label(movimiento['tipo']).classes('w-24')
                ui.label(movimiento['categoria'] or '-').classes('w-32')
                with ui.column().classes('col gap-0'):
                    ui.label(movimiento['descripcion'] or '-')
                    if movimiento['obs']:
                        ui.label(movimiento['obs']).classes('text-caption text-grey')
                ui.label(formatear_pesos(float(movimiento['monto']))) \
                    .classes('w-32 text-right')
                with ui.row().classes('w-28 justify-center no-wrap'):
                    ui.button(
                        icon='edit',
                        on_click=lambda m=movimiento: editar_movimiento(m)
                    ).props('flat round dense').tooltip('Editar movimiento')
                    if movimiento['origen_tipo']:
                        ui.icon('link').classes('text-grey').tooltip(
                            'Vinculado a Insumos o Implementos; no se puede borrar desde Finanzas'
                        )
                    else:
                        ui.button(
                            icon='delete',
                            on_click=lambda m=movimiento: borrar_movimiento(m)
                        ).props('flat round dense color=negative').tooltip('Borrar movimiento')
            ui.separator()


with ui.tabs() as tabs:
    t1 = ui.tab('Lotes'); t2 = ui.tab('Animales'); t3 = ui.tab('Implementos')
    t4 = ui.tab('Calendario'); t5 = ui.tab('Insumos'); t6 = ui.tab('Finanzas')
with ui.tab_panels(tabs, value=t1).classes('w-full'):
    with ui.tab_panel(t1):
        with ui.row().classes('w-full justify-end'):
            ui.icon('map').classes('text-2xl text-primary').tooltip('Lotes')
        with ui.tabs() as lotes_tabs:
            t_mapa = ui.tab('Mapa')
            t_listado = ui.tab('Listado')
        with ui.tab_panels(lotes_tabs, value=t_mapa).classes('w-full'):
            with ui.tab_panel(t_mapa):
                with ui.row().classes('w-full items-end'):
                    localidad = ui.input('Pueblo / ciudad').props('clearable')

                    async def buscar_localidad():
                        consulta = (localidad.value or '').strip()
                        if not consulta:
                            ui.notify('Ingresá un pueblo o ciudad', type='warning')
                            return

                        url = 'https://nominatim.openstreetmap.org/search?' + urlencode({
                            'q': consulta,
                            'countrycodes': 'ar',
                            'format': 'jsonv2',
                            'limit': 1,
                        })
                        solicitud = Request(url, headers={'User-Agent': 'AppCampo/1.0'})

                        try:
                            def consultar():
                                with urlopen(solicitud, timeout=10) as respuesta:
                                    return json.load(respuesta)

                            resultados = await asyncio.to_thread(consultar)
                        except (URLError, TimeoutError, json.JSONDecodeError):
                            ui.notify('No se pudo buscar la localidad. Revisá la conexión.',
                                      type='negative')
                            return

                        if not resultados:
                            ui.notify('No se encontró esa localidad en Argentina', type='warning')
                            return

                        resultado = resultados[0]
                        mapa.set_center((float(resultado['lat']), float(resultado['lon'])))
                        mapa.set_zoom(13)
                        ui.notify(f"Mapa centrado en {resultado['display_name']}", type='positive')

                    ui.button('Buscar en el mapa', on_click=buscar_localidad, icon='search')
                with ui.row().classes('w-full no-wrap'):
                    mapa = ui.leaflet(center=(-35.66, -63.75), zoom=13,
                        draw_control={'draw': {'polygon': True, 'marker': False, 'circle': False,
                            'rectangle': False, 'polyline': False, 'circlemarker': False},
                            'edit': {'edit': False, 'remove': False}}).classes('w-2/3').style('height:80vh')
                    mapa.tile_layer(url_template='https://server.arcgisonline.com/ArcGIS/rest/services/'
                                    'World_Imagery/MapServer/tile/{z}/{y}/{x}', options={'maxZoom': 19})
                    mapa.on('map-click', al_click)
                    mapa.on('draw:created', al_dibujar)
                    with ui.column().classes('w-1/3'):
                        panel_lote()
            with ui.tab_panel(t_listado):
                listado_lotes()

    with ui.tab_panel(t2):
        with ui.row().classes('w-full justify-end'):
            ui.icon('pets').classes('text-2xl text-primary').tooltip('Animales')
        with ui.row().classes('w-full no-wrap'):
            with ui.column().classes('w-1/3'):
                form_animales()
                ui.html('<div style="height: 16px;"></div>')
                resumen_animales()
            with ui.column().classes('w-2/3'):       # lateral derecho
                ui.label('Animales registrados').classes('text-h6')
                selector = ui.select(opciones_animales(), label='Ver animal',
                                    clearable=True, with_input=True,
                                    on_change=elegir_animal).classes('w-full')
                detalle_animal()
                tabla_animales()

    with ui.tab_panel(t3):
        with ui.row().classes('w-full justify-end'):
            ui.icon('agriculture').classes('text-2xl text-primary').tooltip('Implementos')
        with ui.row().classes('w-full no-wrap'):
            with ui.column().classes('w-1/3'):
                form_impl()
                selector_impl = ui.select(opciones_impl(), label='Implemento',
                                          with_input=True, clearable=True,
                                          on_change=elegir_impl).classes('w-full')
            with ui.column().classes('w-2/3'):
                vencimientos()
                panel_impl()

    with ui.tab_panel(t4):
        with ui.row().classes('w-full justify-end'):
            ui.icon('calendar_month').classes('text-2xl text-primary').tooltip('Calendario')
        with ui.element('div').classes('calendar-layout w-full'):
            with ui.column().classes('calendar-sidebar'):
                ui.label('Nueva nota').classes('text-h6')
                fecha_nota = ui.input('Fecha', value=sel_cal['fecha'].isoformat()) \
                    .props('type=date').classes('w-full')
                tipo_nota = ui.select(['Trabajo', 'Proyecto', 'Tarea'], value='Tarea',
                                      label='Tipo').classes('w-full')
                titulo_nota = ui.input('Título').classes('w-full')
                detalle_nota = ui.textarea('Detalle / observaciones').classes('w-full')

                def guardar_nota_calendario():
                    titulo = (titulo_nota.value or '').strip()
                    if not titulo:
                        ui.notify('Ingresá un título para la nota', type='warning')
                        return
                    try:
                        fecha = date.fromisoformat(fecha_nota.value or '')
                    except ValueError:
                        ui.notify('Seleccioná una fecha válida', type='warning')
                        return
                    db.execute(
                        'INSERT INTO tareas_calendario(fecha,tipo,titulo,nota) VALUES(?,?,?,?)',
                        (fecha.isoformat(), tipo_nota.value, titulo, detalle_nota.value))
                    db.commit()
                    sel_cal['mes'] = fecha.replace(day=1)
                    seleccionar_fecha_calendario(fecha)
                    titulo_nota.set_value('')
                    detalle_nota.set_value('')
                    ui.notify('Nota agregada al calendario', type='positive')

                ui.button('Agregar nota', on_click=guardar_nota_calendario,
                          icon='add').classes('w-full')
            with ui.column().classes('calendar-main'):
                panel_calendario()

    with ui.tab_panel(t5):
        with ui.row().classes('w-full justify-end'):
            ui.icon('inventory_2').classes('text-2xl text-primary').tooltip('Insumos')
        with ui.row().classes('w-full no-wrap'):
            with ui.column().classes('w-1/3'):
                form_insumos()
            with ui.column().classes('w-2/3'):
                ui.label('Stock registrado').classes('text-h6')
                selector_insumos = ui.select(opciones_insumos(), label='Seleccionar insumo',
                    with_input=True, clearable=True, on_change=elegir_insumo).classes('w-full')
                tabla_insumos()
                detalle_insumo()

    with ui.tab_panel(t6):
        panel_finanzas()

dibujar_lotes()
ui.run(title='Campo')