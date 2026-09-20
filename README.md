# service-aqualav — API del MVP de AquaLav

Backend del sistema de reservas de un lavadero de autos. Implementa el
subconjunto de requisitos definido en `SRS-AquaLav-MVP-v0.1.md`: registro e
inicio de sesión, control de acceso por permisos, catálogo de servicios con
tarifas históricas, consulta de disponibilidad, creación y cancelación de
reservas, operación del local (check-in, avance de estado, check-out) y
registro del cobro presencial.

- **Stack**: Python 3.12 · FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Alembic ·
  JWT (HS256) · bcrypt (coste 12) · pytest.
- **Documentación interactiva**: `http://localhost:8000/docs` (OpenAPI 3.1).
- **Zona horaria de negocio**: `America/Lima`. Los importes son enteros en
  céntimos y siempre viajan con su moneda (`PEN`).

---

## 1. Arquitectura en capas

El código está separado en cuatro capas con una sola dirección de dependencia:

```
app/
  api/v1/          ← HTTP.  Parsea, delega en un servicio y mapea a un esquema.
  services/        ← Reglas de negocio. Dueñas de la transacción.
  repositories/    ← Acceso a datos. SQL y nada más.
  models/          ← Tablas SQLAlchemy.
  schemas/         ← Contratos de entrada y salida (Pydantic v2).
  core/            ← Seguridad, horarios, errores, códigos.
```

Las reglas que sostienen esa separación:

| Capa | Puede | No puede |
|---|---|---|
| `api/v1` | leer el cuerpo, llamar a un servicio, devolver un esquema | contener reglas de negocio ni consultar la base directamente |
| `services` | decidir, validar, abrir y cerrar la transacción, lanzar `AppError` | importar FastAPI |
| `repositories` | construir consultas, devolver modelos o `None` | lanzar errores HTTP, decidir qué significa un resultado vacío |

**Por qué.** Es lo que permite que la misma regla se pueda probar sin levantar
un servidor, que el cambio de un endpoint no arrastre lógica de negocio y que
las extensiones previstas para v0.2–v1.0 (ver §7) entren sin reescribir capas.
También es lo que evalúa `RNF-015 M1`.

Toda respuesta de error, sin excepción, tiene la misma forma:

```json
{"error": {"codigo": "RESERVA_BLOQUE_OCUPADO", "mensaje": "…", "detalles": [{"campo": "inicio", "mensaje": "…"}]}}
```

El `mensaje` está en español y dice la causa y qué hacer (`RNF-009 M3`).

---

## 2. Levantar todo con Docker (camino recomendado)

Requisitos: Docker y Docker Compose. Desde la raíz del repositorio:

```bash
cp .env.example .env        # en Windows: copy .env.example .env
docker compose up --build
```

> `.env.example` todavía arrastra el nombre de base de datos de la plantilla
> anterior (`ciclox_db`) y un par de variables `SEED_TEST_USER_*` que ya no se
> usan. Para Docker da igual: `docker-compose.yml` fija `DATABASE_URL` apuntando
> al contenedor `db` y la configuración ignora las variables sobrantes. Para el
> arranque sin Docker (§3) sí hay que corregir esa línea.

Eso deja funcionando, en menos de quince minutos en una máquina limpia:

1. `db`: PostgreSQL 16 con volumen persistente y *healthcheck*.
2. `api`: espera a que la base esté sana, aplica las migraciones
   (`alembic upgrade head`), ejecuta la carga inicial (`python -m app.seed`) y
   arranca uvicorn en el puerto `8000`.

Comprobación rápida:

```bash
curl http://localhost:8000/api/v1/health
# {"status":"ok"}
```

Abre `http://localhost:8000/docs` para explorar la API con la documentación
interactiva.

Para apagar todo conservando los datos: `docker compose down`.
Para borrarlos también: `docker compose down -v`.

---

## 3. Ejecutar sin Docker

Necesitas Python 3.12 y un PostgreSQL accesible.

```bash
python -m venv .venv
source .venv/bin/activate        # en Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Ajusta DATABASE_URL en .env, por ejemplo:
# DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/aqualav

createdb aqualav                 # o CREATE DATABASE aqualav; desde psql
alembic upgrade head             # crea el esquema
python -m app.seed               # datos de referencia y usuarios de demo

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` es necesario para abrir la API desde un celular o un emulador.
En el emulador de Android, el host del PC es `10.0.2.2`.

> La aplicación **no** crea tablas por su cuenta: el esquema es responsabilidad
> de Alembic. Si olvidas `alembic upgrade head`, la API arranca pero cada
> consulta falla.

### Servicios externos simulados

Todo servicio externo se alcanza por un puerto (`Protocol`) cuya
implementación predeterminada es local, determinista y **sin red**. Cada opción
trae un valor por defecto que funciona, así que la API arranca sin tocar
`.env`:

| Variable | Valor por defecto | Qué selecciona |
|---|---|---|
| `CORREO_PROVEEDOR` | `simulado` | Envío de correo (`RF-035`: la contraseña temporal; `RF-001`/`RF-003`/`RF-006`: verificación y recuperación). `app/services/proveedores/correo.py` registra el mensaje en el log y lo guarda en memoria para poder leerlo en una demo o en una prueba. Un valor desconocido cae en la simulación a propósito. |

Y estas tres, que no seleccionan un proveedor sino que afinan reglas de
`INC-3`:

| Variable | Valor por defecto | Qué controla |
|---|---|---|
| `VERIFICACION_CORREO_EXPIRA_HORAS` | `24` | Vigencia del enlace de verificación de `RF-001`. No es una credencial, así que una ventana amplia ahorra reenvíos. |
| `RECUPERACION_EXPIRA_MINUTOS` | `30` | Vigencia del enlace de `RF-003`. **El requisito dice 30 literalmente** (`CA-02`): es una variable para poder acortarla en una demo, nunca para relajarla. |
| `URL_BASE_APP` | `https://aqualav.pe/app` | Prefijo del enlace que arma el correo simulado. |
| `EXIGIR_VEHICULO_VERIFICADO` | `false` | `RN-01` v1.0 pide un vehículo «registrado **y verificado**». La regla está implementada (`vehiculo.verificado` + `POST /vehiculos/{id}/verificacion`) y el interruptor viene **apagado**: ningún requisito describe cómo se verifica un vehículo antes de su primera visita, así que exigirlo de fábrica dejaría a un cliente nuevo sin poder reservar la cita que permitiría al mostrador verificar su auto. |

---

## 4. Pruebas

La suite usa SQLite en memoria, así que no necesita PostgreSQL ni Docker:

```bash
pytest                                        # toda la suite
pytest --cov=app --cov-report=term-missing    # con cobertura (RNF-015 M2)
pytest tests/test_reservas.py -k 201_y_un_409 # un caso puntual
```

### Puerta de calidad (`RNF-015 M3`)

Los tres comandos tienen que salir limpios antes de dar un cambio por terminado:

```bash
pytest
ruff check app tests
black --check app tests
```

`ruff` está configurado para no marcar el idiom de inyección de dependencias de
FastAPI (`Depends()`, `Query()`, y las fábricas `requiere_permiso` de
`app/deps.py`) como argumento mutable por defecto: ahí la llamada en tiempo de
importación es intencionada. Ver `[tool.ruff.lint.flake8-bugbear]` en
`pyproject.toml`.

Cada prueba levanta su propia base sembrada y un `TestClient` autenticado como
cliente, recepcionista, operario o administrador. Las pruebas cubren, como mínimo, todos los
criterios de aceptación (`CA-nn`) de los requisitos implementados:

| Archivo | Cubre |
|---|---|
| `test_auth.py` | RF-001 CA-01/02/03 · RF-002 CA-01/02 · refresco de token |
| `test_vehiculos.py` | RF-007 CA-01/02/03 |
| `test_servicios.py` | RF-009 CA-01/02 · RF-010 CA-01/02/03 |
| `test_tarifas.py` | RF-012 CA-01/02 y flujos `3a`/`4a` · RF-011 CA-01/02 y flujos `2a`/`4a` · RF-009 CA-02 v1.0 · RF-010 v1.0 (factores auditados) · RN-04 · RN-12 |
| `test_disponibilidad.py` | RF-013 CA-01/02/03 · sugerencia de siguiente fecha |
| `test_reservas.py` | RF-014 CA-01/02/03 · RF-016 CA-01/02/03 · RF-017 CA-01/02/03 |
| `test_operacion.py` | RF-019 CA-01/02/03 · RF-021 CA-01/02/03 · RF-022 CA-02 · RF-024 CA-01/02 |
| `test_pagos.py` | RF-026 CA-01/02/03 |
| `test_rbac.py` | RF-004 CA-01/02/**03** (inspección de código automatizada) |
| `test_roles.py` | RF-004 v1.0: catálogo de roles y permisos, asignación de rol, flujos 3a y 4a, CA-02 (bitácora con el valor anterior) |

`SELECT … FOR UPDATE` no hace nada en SQLite: por eso la prueba de
concurrencia afirma el **resultado** (exactamente un `201` y un `409`) y nunca
el mecanismo de bloqueo.

---

## 5. Credenciales de demostración

Las crea el seed y se configuran en `.env`. **Son solo para desarrollo.**

| Rol | Correo | Contraseña | Aterriza en |
|---|---|---|---|
| Administrador | `admin@aqualav.pe` | `Admin1234` | catálogo de servicios |
| Recepcionista | `recepcion@aqualav.pe` | `Recepcion1234` | mostrador (recepción y entrega) |
| Operario | `operario@aqualav.pe` | `Operario1234` | cola de la bahía |
| Cliente | `cliente@aqualav.pe` | `Cliente1234` | inicio del cliente |

Todas tienen un valor por defecto en `app/config.py`, así que el sistema
arranca sin tocar `.env`; las claves de configuración son
`SEED_RECEPCION_CORREO`, `SEED_RECEPCION_PASSWORD`, `SEED_OPERARIO_CORREO` y
`SEED_OPERARIO_PASSWORD`.

El seed también carga los 21 permisos, los 4 roles, las 14 transiciones de
estado del Anexo A v1.0, las 4 bahías, 5 servicios con su precio vigente y un
vehículo de demostración (`ABC-123`) para el cliente. Desde `INC-2` añade la
tarifa completa de `RN-04`: los **factores por tipo de vehículo** (sedán 1,0 ·
SUV 1,3 · camioneta 1,4 · motocicleta 0,8, más un factor propio del «Lavado
Express» para motocicletas), tres **adicionales**, un **paquete** («Pack Brillo
Total», S/ 60,00 frente a S/ 70,00 por separado) y tres **promociones**: una
vigente, una ya vencida —para ver que caduca sola— y el cupón `BIENVENIDA10`.
Es idempotente: se puede ejecutar tantas veces como haga falta.

---

## 6. Endpoints

Todo cuelga de `/api/v1`. La autenticación es `Authorization: Bearer <access>`.

| Método | Ruta | Permiso | RF | Códigos |
|---|---|---|---|---|
| `POST` | `/auth/registro` | público | RF-001 | 201 · 409 · 422 |
| `POST` | `/auth/login` | público | RF-002 | 200 · 401 · 429 |
| `POST` | `/auth/refresh` | público | RF-002, RF-005 | 200 · 401 |
| `POST` | `/auth/logout` | público | RF-005 `CA-01` | 200 |
| `POST` | `/auth/verificacion` | público | RF-001, RF-006 `3a` | 200 · **400** |
| `POST` | `/auth/verificacion/reenviar` | público | RF-001 `5a`, RF-002 `2c` | 200 |
| `POST` | `/auth/password/recuperacion` | público | RF-003 `2a` | 200 |
| `POST` | `/auth/password/restablecer` | público | RF-003 `CA-01`, `CA-02` | 200 · **400** · 422 |
| `GET` | `/auth/yo` | autenticado | RF-002 | 200 · 401 |
| `GET` | `/perfil` | autenticado | RF-006 | 200 · 401 |
| `PATCH` | `/perfil` | autenticado | RF-006 `CA-01`, `CA-02` | 200 · 401 · 409 · 422 |
| `GET` | `/vehiculos?incluir_inactivos=` | `vehiculo:leer` | RF-007, RF-008 `CA-02` | 200 · 401 · 403 |
| `POST` | `/vehiculos` | `vehiculo:crear` | RF-007 | 201 · 409 · 422 |
| `PATCH` | `/vehiculos/{id}` | `vehiculo:editar` | RF-008 | 200 · 404 · 409 · 422 |
| `DELETE` | `/vehiculos/{id}` | `vehiculo:eliminar` | RF-008 `CA-01`, `3a` | 200 · 404 · **409** |
| `POST` | `/vehiculos/{id}/verificacion` | `vehiculo:verificar` | RN-01 | 200 · 403 · 404 |
| `GET` | `/servicios?vehiculo_id=&tipo_vehiculo=` | `servicio:leer` | RF-009 | 200 · 401 · 403 · 404 |
| `GET` | `/servicios/{id}?vehiculo_id=&tipo_vehiculo=` | `servicio:leer` | RF-009 `CA-02` | 200 · 404 |
| `GET` | `/paquetes` | `servicio:leer` | RF-011 | 200 · 403 |
| `GET` | `/promociones` | `servicio:leer` | RF-011 | 200 · 403 |
| `GET` | `/adicionales` | `servicio:leer` | RN-04 | 200 · 403 |
| `POST` | `/tarifas/calculo` | `servicio:leer` | RF-012 | 200 · 404 · **422** |
| `GET` | `/admin/servicios` | `servicio:administrar` | RF-010 | 200 · 403 |
| `GET` | `/admin/servicios/{id}` | `servicio:administrar` | RF-010 | 200 · 403 · 404 |
| `POST` | `/admin/servicios` | `servicio:administrar` | RF-010 | 201 · 403 · 422 |
| `PATCH` | `/admin/servicios/{id}` | `servicio:administrar` | RF-010 | 200 · 403 · 404 · 422 |
| `GET` | `/admin/factores` | `servicio:administrar` | RF-010 v1.0 | 200 · 403 |
| `PUT` | `/admin/factores` | `servicio:administrar` | RF-010 v1.0, RN-04 | 200 · 403 · 404 · 422 |
| `GET` | `/admin/paquetes` | `promocion:administrar` | RF-011 | 200 · 403 |
| `POST` | `/admin/paquetes` | `promocion:administrar` | RF-011 | 201 · 403 · 404 · 422 |
| `PATCH` | `/admin/paquetes/{id}` | `promocion:administrar` | RF-011 | 200 · 404 · 422 |
| `GET` | `/admin/promociones` | `promocion:administrar` | RF-011 | 200 · 403 |
| `POST` | `/admin/promociones` | `promocion:administrar` | RF-011 `2a` | 201 · **409** · 404 · 422 |
| `PATCH` | `/admin/promociones/{id}` | `promocion:administrar` | RF-011 `2a` | 200 · **409** · 404 · 422 |
| `GET` | `/admin/adicionales` | `promocion:administrar` | RN-04 | 200 · 403 |
| `POST` | `/admin/adicionales` | `promocion:administrar` | RN-04 | 201 · 403 · 422 |
| `PATCH` | `/admin/adicionales/{id}` | `promocion:administrar` | RN-04 | 200 · 404 · 422 |
| `GET` | `/admin/roles` | `rol:administrar` | RF-004 | 200 · 401 · 403 |
| `GET` | `/admin/permisos` | `rol:administrar` | RF-004 | 200 · 401 · 403 |
| `PUT` | `/admin/usuarios/{id}/rol` | `rol:administrar` | RF-004 | 200 · 403 · 404 · **422** |
| `GET` | `/admin/usuarios?rol_id=&estado_cuenta=` | `usuario:administrar` | RF-035 | 200 · 403 |
| `POST` | `/admin/usuarios` | `usuario:administrar` | RF-035 | 201 · **409** · 422 |
| `PATCH` | `/admin/usuarios/{id}` | `usuario:administrar` | RF-035 | 200 · **409** · 404 · 422 |
| `GET` | `/admin/bahias` | `bahia:administrar` | RF-018, RF-020 | 200 · 403 |
| `POST` | `/admin/bahias` | `bahia:administrar` | RE-07 | 201 · **409** · **422** |
| `PATCH` | `/admin/bahias/{id}` | `bahia:administrar` | RE-07 | 200 · **409** · 404 |
| `GET` | `/agenda?fecha=&vista=dia\|semana` | `agenda:leer` | RF-018 | 200 · 403 |
| `GET` | `/agenda/horarios` | `agenda:leer` | RF-018, RN-07 | 200 · 403 |
| `PUT` | `/agenda/horarios/{dia_semana}` | `agenda:administrar` | RF-018 | 200 · 403 · 422 |
| `GET` | `/agenda/dias-no-laborables?desde=&hasta=` | `agenda:leer` | RF-018 | 200 · 403 |
| `POST` | `/agenda/dias-no-laborables` | `agenda:administrar` | RF-018 | 201 · **409** · 422 |
| `DELETE` | `/agenda/dias-no-laborables/{id}` | `agenda:administrar` | RF-018 | 204 · 404 |
| `GET` | `/agenda/bloqueos?desde=&hasta=` | `agenda:leer` | RF-018 | 200 · 403 |
| `POST` | `/agenda/bloqueos` | `agenda:administrar` | RF-018 `4a` | 201 · **409** · 422 |
| `DELETE` | `/agenda/bloqueos/{id}` | `agenda:administrar` | RF-018 | 204 · 404 |
| `GET` | `/estados` | autenticado | P3 | 200 |
| `GET` | `/disponibilidad?fecha=&servicio_id=` | `disponibilidad:leer` | RF-013 | 200 · 404 |
| `POST` | `/reservas` | `reserva:crear` | RF-014 | 201 · 404 · **409** · 422 |
| `GET` | `/reservas?estado=&pagina=&tamanio=` | `reserva:leer_propias` o `reserva:leer_todas` | RF-017 | 200 · 403 |
| `GET` | `/reservas/{id}` | idem | RF-017, RF-022 | 200 · 404 |
| `GET` | `/reservas/buscar?codigo=&placa=&qr=` | `reserva:check_in` | RF-019 | 200 · 404 |
| `POST` | `/reservas/atencion-inmediata` | `reserva:check_in` | RF-019 `1a` | 201 · **409** · 422 |
| `POST` | `/reservas/{id}/cancelacion` | `reserva:cancelar` | RF-016 | 200 · **422** |
| `POST` | `/reservas/{id}/check-in` | `reserva:check_in` | RF-019 | 200 · **409** · 422 |
| `POST` | `/reservas/{id}/estado` | `reserva:avanzar_estado` | RF-021 | 200 · **422** |
| `POST` | `/reservas/{id}/asignacion` | `reserva:asignar` | RF-020 | 200 · **409** · 422 |
| `GET` | `/reservas/cola` | `reserva:avanzar_estado` | RF-020 `CA-02` | 200 · 403 |
| `POST` | `/reservas/{id}/revision` | `reserva:revisar` | RF-024 `3a` | 200 · **422** |
| `POST` | `/reservas/{id}/check-out` | `reserva:check_out` | RF-024 | 200 · **422** |
| `POST` | `/reservas/{id}/pagos` | `pago:registrar` | RF-026 | **201 / 200** · 400 · 422 |
| `GET` | `/api/v1/health` | público | RNF-010 | 200 |

**La autorización es siempre por permiso, nunca por nombre de rol** (principio
`P5`, `RF-004 CA-03`). Los cuatro roles de la v1.0 (`cliente`, `recepcionista`,
`operario`, `administrador`) salieron de dividir `personal` en filas de `rol` y
`rol_permiso`: ningún endpoint cambió, y `PUT /admin/usuarios/{id}/rol` asigna
el rol **por identificador**, para que tampoco el cliente móvil tenga que
escribir un nombre de rol. Además existe autorización horizontal: quien tiene
`reserva:leer_propias` pero no `reserva:leer_todas` solo ve sus propias
reservas.

El cambio de rol se aplica en la siguiente petición (los permisos se leen de la
base, no del token), escribe un evento de dominio con el valor anterior y el
nuevo, y llama al gancho que invalidará los tokens de refresco del afectado
—la revocación real llega con `RF-005`—. Un administrador no puede quitarse a
sí mismo `rol:administrar` (`422 CAMBIO_DE_ROL_PROPIO`).

### Reglas de negocio que verás en las respuestas

- `RN-02` — una reserva necesita 60 minutos de anticipación.
- `RN-03` — una bahía atiende un vehículo a la vez; el solapamiento se evalúa
  contra las reservas en cualquier estado **no terminal**, derivado de
  `transicion_estado` y nunca listado en código.
- `RN-07` — horario de atención: lunes a sábado 08:00–19:00, domingos
  09:00–14:00. El intervalo completo debe caber en la ventana del día.
  **Desde `RF-018` el horario es un DATO**: vive en `horario_atencion`, los
  feriados en `dia_no_laborable` y los cierres parciales en `bloqueo_franja`.
  `app/core/horario.py` sigue siendo puro —no abre sesiones—: el servicio lee
  las filas, arma un `Calendario` y se lo pasa. Por eso `es_laborable()` ya no
  devuelve siempre `True`, y una franja bloqueada desaparece de la
  disponibilidad de `RF-013` (`RF-018 CA-01`).
- `RF-020` — al asignar, la bahía pasa a `ocupada` y el servicio entra en la
  cola del operario. Sin bahía libre la reserva **no avanza**: queda en
  `cola_espera` con un tiempo estimado (`2a`). Si el operario elegido ya tiene
  trabajo, hace falta `confirmar_operario_ocupado` (`3a`). La bahía se libera
  sola cuando la reserva llega a un estado **terminal**, que se deriva de
  `transicion_estado`, nunca de una lista de estados.
- `RN-04` — `tarifa = (precio base × factor por tipo de vehículo) + adicionales
  − descuentos`. El resultado viaja **desglosado** en `tarifa` (base, factor,
  adicionales, descuento y total) y se **congela** en la reserva al crearla, en
  la tabla `reserva_tarifa_desglose`: cambiar después el precio, el factor o la
  promoción no mueve ni un céntimo de lo ya reservado. El factor se guarda en
  **milésimas** (`1300` = 1,3) y todo importe es un entero de céntimos, así que
  `3000 × 1,3` son exactamente `3900` (`RF-012 CA-01`) y no el `3899,99…` que
  devolvería un `float`. Un cupón inválido o vencido **no tumba la operación**:
  se informa el motivo en `cupon_rechazado` / `motivo_rechazo_cupon` y el total
  se recalcula sin él (`3a`); si el descuento superara el total, la tarifa se
  limita a cero y queda la incidencia (`4a`). Ambos casos escriben un evento de
  dominio.
- `RF-011` — una promoción sin cupón se aplica sola y **no puede solaparse en
  fechas** con otra automática del mismo servicio (`409 PROMOCION_SOLAPADA`,
  que nombra la promoción en conflicto para poder ajustar el rango). Los
  cupones sí pueden convivir: los elige el cliente escribiéndolos. Una
  promoción vencida deja de aplicarse **sola**, porque la vigencia se compara
  con la fecha del servicio en cada cálculo; nadie la desactiva.
- `RN-09` — no se entrega un vehículo sin servicio finalizado y pago confirmado.
- `RN-12` — soles con IGV incluido; el precio almacenado es el final.

---

## 7. Puntos de extensión

Tres decisiones del MVP existen únicamente para que las versiones siguientes
sean una migración de datos y no una reescritura. Son deliberadas y están
señaladas en el código con el comentario `EXTENSION POINT`.

### P3 — La máquina de estados vive en `transicion_estado`

La tabla guarda las transiciones válidas con el permiso que cada una exige y
el endpoint que es dueño del movimiento. Estas son las catorce filas del Anexo
A v1.0:

| estado_origen | estado_destino | permiso_requerido | endpoint dueño |
|---|---|---|---|
| `pendiente_pago` | `confirmada` | `pago:registrar` | `pago` |
| `pendiente_pago` | `cancelada` | `reserva:cancelar` | `cancelacion` |
| `confirmada` | `en_recepcion` | `reserva:check_in` | `check_in` |
| `confirmada` | `cancelada` | `reserva:cancelar` | `cancelacion` |
| `en_recepcion` | `asignado` | `reserva:asignar` | `asignacion` |
| `en_recepcion` | `cancelada` | `reserva:cancelar` | `cancelacion` |
| `asignado` | `en_lavado` | `reserva:avanzar_estado` | — |
| `en_lavado` | `secado` | `reserva:avanzar_estado` | — |
| `secado` | `acabado` | `reserva:avanzar_estado` | — |
| `acabado` | `finalizado` | `reserva:avanzar_estado` | — (marca fin de servicio) |
| `finalizado` | `entregado` | `reserva:check_out` | `check_out` |
| `finalizado` | `en_revision` | `reserva:revisar` | `revision` |
| `en_revision` | `acabado` | `reserva:avanzar_estado` | — |
| `en_revision` | `entregado` | `reserva:check_out` | `check_out` |

Las otras cuatro transiciones del Anexo A no son filas: dos son la **creación**
de la reserva (no hay estado de origen) y dos son los **sumideros terminales**,
que se deducen de la ausencia de filas salientes.

`operacion_service.cambiar_estado` **lee esta tabla en cada intento**; ninguna
línea de código enumera los estados permitidos, y desde INC-1A tampoco el
destino: el check-in, el check-out y la cancelación preguntan a la tabla a
dónde llevan (`destino_declarado`). La prueba
`test_ningun_modulo_compara_el_nombre_de_un_estado` recorre el paquete `app`
con el módulo `ast` y falla si algún módulo compara con un estado concreto. La respuesta de una reserva
incluye `transiciones_permitidas`, ya filtrado por los permisos de quien
pregunta, y la app móvil dibuja sus botones a partir de ese arreglo.

*Cómo creció a v1.0*: los once estados de la versión completa entraron como
filas en la migración `0003_estados_roles_v1`, que además convirtió los datos
existentes (`en_atencion` → `en_lavado`). Ni un `elif` nuevo. La prueba
`test_una_transicion_nueva_en_la_tabla_funciona_sin_tocar_codigo` lo sigue
demostrando insertando una transición inédita y usándola en el mismo test, y
`test_el_checkin_aterriza_donde_diga_la_tabla` hace lo propio con un estado
destino que no existe en ningún enum.

### P7 — Toda mutación deja rastro: `reserva_estado_historial` y `evento_dominio`

Cada cambio de estado escribe una fila en `reserva_estado_historial` con el
estado, el autor y la marca de tiempo, y cada mutación del dominio (registro de
usuario, alta de vehículo, cambio de precio, creación y cancelación de reserva,
check-in, check-out, pago) escribe una fila en `evento_dominio`. Nada en el MVP
**lee** esas tablas, y eso es intencional.

*Cómo crece a v1.0*: la línea de tiempo de `RF-022` ya se sirve del historial;
los reportes de ocupación y facturación de v1.0 y las notificaciones de
`RF-029` (v0.2) se alimentan del registro de eventos. Rellenar ese historial
hacia atrás sería imposible: por eso se escribe desde el primer día. El puerto
`Notificador` (`services/notificador.py`) ya está conectado en los servicios de
reserva y operación; v0.2 solo registra una segunda implementación que empuja
por push.

### P6 — Idempotencia y precios históricos

Dos piezas del mismo principio: no sobrescribir información que costará dinero
recuperar.

- **`pago.idempotency_key`** es obligatoria desde el MVP aunque el cobro sea
  presencial y nadie reintente. Repetir la clave devuelve el pago original con
  `200` y no crea una segunda fila.
  *Cómo crece a v0.3*: cuando entre la pasarela de pagos, esa clave es lo que
  evita el doble cobro ante un reintento de red. Añadirla después obligaría a
  migrar todos los pagos ya registrados.
- **`servicio_precio`** guarda la historia de tarifas. Cambiar un precio
  **cierra** la fila vigente (`vigente_hasta = ahora`) e **inserta** una nueva;
  el monto nunca se actualiza. La reserva congela su importe al crearse.
  *Cómo creció a v1.0*: exactamente como estaba previsto. `INC-2` añadió
  `factor_tipo_vehiculo`, versionado con el mismo patrón (un cambio **cierra**
  la fila vigente e inserta otra, y se audita con el valor anterior y el
  nuevo), y `precio_aplicable` / `precio_promocional` entraron como campos
  nuevos del objeto `precio` sin romper a ningún cliente. Una reserva antigua
  conserva su tarifa (`RF-010 CA-02`) y ahora también **el desglose que la
  explica** (`reserva_tarifa_desglose`), que es lo que `RF-012` exige que sea
  trazable y auditable.

---

## 8. Estructura del repositorio

```
app/
  main.py                  FastAPI, CORS, manejadores de error, routers, lifespan
  config.py                configuración por variables de entorno
  database.py              engine, SessionLocal, Base, get_db
  deps.py                  usuario_actual, permisos_actuales, requiere_permiso
  core/                    security · password · errors · horario · codigos
  models/                  una tabla por módulo
  schemas/                 contratos Pydantic v2
  repositories/            acceso a datos por agregado
  services/                auth · rol · usuario · vehiculo · servicio · bahia
                           disponibilidad · agenda · asignacion · reserva
                           operacion · pago · tarifa · promocion · eventos
                           notificador · politica_cancelacion · ensamblador
                           proveedores/ (correo simulado)
  api/v1/                  auth · vehiculos · servicios · catalogo
                           admin_servicios · admin_tarifas · admin_roles
                           admin_usuarios · admin_bahias · agenda
                           disponibilidad · reservas · operacion · asignacion
                           pagos · estados · health
  seed.py                  carga inicial idempotente
migrations/                Alembic
tests/                     pytest sobre SQLite en memoria
Dockerfile · docker-compose.yml · docker-entrypoint.sh
```

---

## 9. Notas de seguridad

- Las contraseñas se almacenan con bcrypt de coste 12 y nunca se devuelven en
  ninguna respuesta.
- Cinco intentos fallidos consecutivos bloquean la cuenta 15 minutos; la
  respuesta `429` indica cuánto falta.
- El token de acceso dura 30 minutos y el de refresco 7 días. Presentar uno
  donde se espera el otro se rechaza.
- Los valores de `.env.example` son de desarrollo. Antes de cualquier
  despliegue real hay que cambiar `SECRET_KEY`, `CORS_ORIGINS` y las
  credenciales de la base de datos, y desactivar el seed (`SEED_ENABLED=false`).
