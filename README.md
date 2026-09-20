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
cliente, personal o administrador. Las pruebas cubren, como mínimo, todos los
criterios de aceptación (`CA-nn`) de los requisitos implementados:

| Archivo | Cubre |
|---|---|
| `test_auth.py` | RF-001 CA-01/02/03 · RF-002 CA-01/02 · refresco de token |
| `test_vehiculos.py` | RF-007 CA-01/02/03 |
| `test_servicios.py` | RF-009 CA-01/02 · RF-010 CA-01/02/03 |
| `test_disponibilidad.py` | RF-013 CA-01/02/03 · sugerencia de siguiente fecha |
| `test_reservas.py` | RF-014 CA-01/02/03 · RF-016 CA-01/02/03 · RF-017 CA-01/02/03 |
| `test_operacion.py` | RF-019 CA-01/02/03 · RF-021 CA-01/02/03 · RF-022 CA-02 · RF-024 CA-01/02 |
| `test_pagos.py` | RF-026 CA-01/02/03 |
| `test_rbac.py` | RF-004 CA-01/02/**03** (inspección de código automatizada) |

`SELECT … FOR UPDATE` no hace nada en SQLite: por eso la prueba de
concurrencia afirma el **resultado** (exactamente un `201` y un `409`) y nunca
el mecanismo de bloqueo.

---

## 5. Credenciales de demostración

Las crea el seed y se configuran en `.env`. **Son solo para desarrollo.**

| Rol | Correo | Contraseña | Aterriza en |
|---|---|---|---|
| Administrador | `admin@aqualav.pe` | `Admin1234` | catálogo de servicios |
| Personal | `personal@aqualav.pe` | `Personal1234` | pantalla de operación |
| Cliente | `cliente@aqualav.pe` | `Cliente1234` | inicio del cliente |

El seed también carga los 13 permisos, los 3 roles, las 4 transiciones de
estado, las 4 bahías, 5 servicios con su precio vigente y un vehículo de
demostración (`ABC-123`) para el cliente. Es idempotente: se puede ejecutar
tantas veces como haga falta.

---

## 6. Endpoints

Todo cuelga de `/api/v1`. La autenticación es `Authorization: Bearer <access>`.

| Método | Ruta | Permiso | RF | Códigos |
|---|---|---|---|---|
| `POST` | `/auth/registro` | público | RF-001 | 201 · 409 · 422 |
| `POST` | `/auth/login` | público | RF-002 | 200 · 401 · 429 |
| `POST` | `/auth/refresh` | público | RF-002 | 200 · 401 |
| `GET` | `/auth/yo` | autenticado | RF-002 | 200 · 401 |
| `GET` | `/vehiculos` | `vehiculo:leer` | RF-007 | 200 · 401 · 403 |
| `POST` | `/vehiculos` | `vehiculo:crear` | RF-007 | 201 · 409 · 422 |
| `GET` | `/servicios` | `servicio:leer` | RF-009 | 200 · 401 · 403 |
| `GET` | `/servicios/{id}` | `servicio:leer` | RF-009 | 200 · 404 |
| `GET` | `/admin/servicios` | `servicio:administrar` | RF-010 | 200 · 403 |
| `POST` | `/admin/servicios` | `servicio:administrar` | RF-010 | 201 · 403 · 422 |
| `PATCH` | `/admin/servicios/{id}` | `servicio:administrar` | RF-010 | 200 · 403 · 404 · 422 |
| `GET` | `/estados` | autenticado | P3 | 200 |
| `GET` | `/disponibilidad?fecha=&servicio_id=` | `disponibilidad:leer` | RF-013 | 200 · 404 |
| `POST` | `/reservas` | `reserva:crear` | RF-014 | 201 · 404 · **409** · 422 |
| `GET` | `/reservas?estado=&pagina=&tamanio=` | `reserva:leer_propias` o `reserva:leer_todas` | RF-017 | 200 · 403 |
| `GET` | `/reservas/{id}` | idem | RF-017, RF-022 | 200 · 404 |
| `GET` | `/reservas/buscar?codigo=&placa=` | `reserva:check_in` | RF-019 | 200 · 404 |
| `POST` | `/reservas/{id}/cancelacion` | `reserva:cancelar` | RF-016 | 200 · **422** |
| `POST` | `/reservas/{id}/check-in` | `reserva:check_in` | RF-019 | 200 · **409** · 422 |
| `POST` | `/reservas/{id}/estado` | `reserva:avanzar_estado` | RF-021 | 200 · **422** |
| `POST` | `/reservas/{id}/check-out` | `reserva:check_out` | RF-024 | 200 · **422** |
| `POST` | `/reservas/{id}/pagos` | `pago:registrar` | RF-026 | **201 / 200** · 400 · 422 |
| `GET` | `/api/v1/health` | público | RNF-010 | 200 |

**La autorización es siempre por permiso, nunca por nombre de rol** (principio
`P5`, `RF-004 CA-03`). Añadir los roles `Recepcionista` y `Operario` en v0.4 es
insertar filas en `rol` y `rol_permiso`: ningún endpoint cambia. Además existe
autorización horizontal: quien tiene `reserva:leer_propias` pero no
`reserva:leer_todas` solo ve sus propias reservas.

### Reglas de negocio que verás en las respuestas

- `RN-02` — una reserva necesita 60 minutos de anticipación.
- `RN-03` — una bahía atiende un vehículo a la vez; el solapamiento se evalúa
  contra las reservas en `confirmada` o `en_atencion`.
- `RN-07` — horario de atención: lunes a sábado 08:00–19:00, domingos
  09:00–14:00. El intervalo completo debe caber en la ventana del día.
- `RN-09` — no se entrega un vehículo sin servicio finalizado y pago confirmado.
- `RN-12` — soles con IGV incluido; el precio almacenado es el final.

---

## 7. Puntos de extensión

Tres decisiones del MVP existen únicamente para que las versiones siguientes
sean una migración de datos y no una reescritura. Son deliberadas y están
señaladas en el código con el comentario `EXTENSION POINT`.

### P3 — La máquina de estados vive en `transicion_estado`

La tabla guarda las transiciones válidas con el permiso que cada una exige:

| estado_origen | estado_destino | permiso_requerido |
|---|---|---|
| `confirmada` | `en_atencion` | `reserva:check_in` |
| `confirmada` | `cancelada` | `reserva:cancelar` |
| `en_atencion` | `finalizado` | `reserva:avanzar_estado` |
| `finalizado` | `entregado` | `reserva:check_out` |

`operacion_service.cambiar_estado` **lee esta tabla en cada intento**; ninguna
línea de código enumera los estados permitidos. La respuesta de una reserva
incluye `transiciones_permitidas`, ya filtrado por los permisos de quien
pregunta, y la app móvil dibuja sus botones a partir de ese arreglo.

*Cómo crece a v1.0*: los once estados de la versión completa y sus nuevas
transiciones (`pendiente_pago`, `en_secado`, `en_control_calidad`, …) se
insertan como filas. Basta reiniciar la API; no hay despliegue de código. La
prueba `test_una_transicion_nueva_en_la_tabla_funciona_sin_tocar_codigo` lo
demuestra insertando una transición inédita y usándola en el mismo test.

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
  *Cómo crece a v1.0*: es lo que permite que una reserva antigua conserve su
  tarifa (`RF-010 CA-02`), que los reportes de ingresos sean correctos y que
  v0.3 agregue `precio_regular`, `precio_promocional` y el factor por tipo de
  vehículo (`RF-012`) como filas y campos nuevos, sin romper al cliente: la API
  ya devuelve `precio` como objeto y no como número suelto.

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
  services/                auth · vehiculo · servicio · disponibilidad · reserva
                           operacion · pago · eventos · notificador
                           politica_cancelacion · ensamblador
  api/v1/                  auth · vehiculos · servicios · admin_servicios
                           disponibilidad · reservas · operacion · pagos · health
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
