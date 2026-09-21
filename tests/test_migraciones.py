"""La cadena de Alembic, ejercida por la suite (INC-8B).

Hasta aquí el esquema tenía **dos descripciones** y solo una se probaba:
``tests/conftest.py`` levanta la base con ``Base.metadata.create_all()``, así
que las 0001→0012 solo se validaban a mano. Una cadena que nadie ejecuta es
una cadena que se rompe en silencio, y la deriva que encontró la auditoría
—dos `DEFAULT` que estaban en la migración y no en el ORM— es exactamente lo
que pasa cuando las dos descripciones no se comparan nunca.

Aquí se comprueban las tres cosas que importan:

1. ``upgrade head`` levanta el esquema entero desde cero;
2. ``downgrade base`` lo desmonta entero, sin dejar una sola tabla. Es la
   mitad que casi nunca se prueba y la que más se rompe: basta que una
   migración olvide un ``drop_index``;
3. el esquema que producen las migraciones y el que produce el ORM **dicen lo
   mismo**, comparados con la misma maquinaria que usa ``alembic revision
   --autogenerate``.

Corre sobre SQLite, como el resto de la suite. Eso acota lo que puede ver y
conviene decirlo en voz alta en vez de fingir lo contrario:

* SQLite no distingue ``json`` de ``jsonb`` (ambos son afinidad ``TEXT``), así
  que la alineación de ``transaccion_pasarela.solicitud``/``.respuesta`` que
  hace la migración ``0012`` es un hecho de PostgreSQL que esta comparación no
  puede confirmar. Queda verificado leyendo el SQL que Alembic emite en modo
  offline contra ``postgresql`` (ver el README, «Migraciones»);
* las dos migraciones que crearon columnas booleanas las deletrearon distinto:
  la ``0001`` con ``sa.text("true")`` —que llega al DDL como ``DEFAULT true``—
  y la ``0006`` con ``sa.true()``, que en SQLite llega como ``DEFAULT 1``.
  Ninguna se puede reescribir (regla 1.8 del plan), así que el ORM copia cada
  una **exactamente como está**: ``text("true")`` en las tres columnas de la
  ``0001`` y ``true()``/``false()`` en las de la ``0006``. Es una incoherencia
  del esquema, no del ORM, y copiarla es lo que permite que la comparación de
  abajo **no tenga ni una excepción**: se exige lista vacía, sin filtros ni
  normalizaciones que puedan tapar una deriva de verdad.
"""

import pathlib
import tempfile

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text

from app.database import Base
from app.models import Permiso, Rol

RAIZ = pathlib.Path(__file__).resolve().parent.parent

#: Alembic's own bookkeeping table. It is created by ``upgrade`` and is not
#: part of the model, so it is not drift.
TABLA_DE_VERSIONES = "alembic_version"


def _configuracion() -> Config:
    configuracion = Config(str(RAIZ / "alembic.ini"))
    configuracion.set_main_option("script_location", str(RAIZ / "migrations"))
    return configuracion


@pytest.fixture()
def motor_vacio():
    """A throwaway SQLite FILE, not ``:memory:``.

    Alembic opens and closes connections of its own around a migration, and an
    in-memory database dies with the connection that made it.
    """
    with tempfile.TemporaryDirectory() as carpeta:
        motor = create_engine(f"sqlite+pysqlite:///{pathlib.Path(carpeta) / 'cadena.sqlite'}")
        try:
            yield motor
        finally:
            motor.dispose()


def _correr(motor, destino: str, *, desde: str | None = None) -> None:
    """``upgrade``/``downgrade`` to ``destino`` over ``motor``."""
    configuracion = _configuracion()
    with motor.begin() as conexion:
        configuracion.attributes["connection"] = conexion
        if desde is None:
            command.upgrade(configuracion, destino)
        else:
            command.downgrade(configuracion, destino)


def _tablas(motor) -> set[str]:
    return set(inspect(motor).get_table_names()) - {TABLA_DE_VERSIONES}


# --------------------------------------------------------------------------
# 1 y 2 - la cadena sube y baja entera
# --------------------------------------------------------------------------
def test_upgrade_head_levanta_el_esquema_completo(motor_vacio):
    """Regla 1.8 del plan: la cadena 0001→head se ejecuta de verdad.

    No se comprueba «que no lance»: se comprueba que el esquema resultante
    tiene todas las tablas que el ORM declara. Una migración que se salta una
    tabla no falla, simplemente deja la base a medias.
    """
    _correr(motor_vacio, "head")

    del_orm = set(Base.metadata.tables)
    de_las_migraciones = _tablas(motor_vacio)

    assert del_orm - de_las_migraciones == set(), "faltan tablas en las migraciones"
    assert de_las_migraciones - del_orm == set(), "las migraciones crean tablas que el ORM no tiene"


def test_downgrade_base_desmonta_el_esquema_entero(motor_vacio):
    """La mitad que casi nunca se prueba.

    Un ``downgrade`` incompleto no se nota nunca en producción —nadie baja— y
    se nota siempre en la siguiente migración, cuando un índice huérfano choca
    con uno nuevo del mismo nombre.
    """
    _correr(motor_vacio, "head")
    assert _tablas(motor_vacio), "la subida debería haber creado algo"

    _correr(motor_vacio, "base", desde="head")

    assert _tablas(motor_vacio) == set()


def test_la_cadena_aguanta_subir_bajar_y_volver_a_subir(motor_vacio):
    """Idempotencia de ida y vuelta: ``head → base → head``.

    Es el escenario real de una rama que se prueba, se descarta y se vuelve a
    aplicar. Si un ``downgrade`` deja basura, la segunda subida es la que se
    entera.
    """
    _correr(motor_vacio, "head")
    _correr(motor_vacio, "base", desde="head")
    _correr(motor_vacio, "head")

    assert set(Base.metadata.tables) <= _tablas(motor_vacio)


# --------------------------------------------------------------------------
# 3 - el ORM y las migraciones dicen lo mismo
# --------------------------------------------------------------------------
def _diferencias(motor) -> list:
    """Drift between the migrated schema and ``Base.metadata``, filtered.

    ``compare_metadata`` is the engine behind ``alembic revision
    --autogenerate``: whatever it would write into a new migration is, by
    definition, something the two descriptions disagree about.
    """
    with motor.connect() as conexion:
        contexto = MigrationContext.configure(
            conexion,
            opts={"compare_type": True, "compare_server_default": True},
        )
        crudas = compare_metadata(contexto, Base.metadata)

    aplanadas: list = []
    for diferencia in crudas:
        # A column-level change arrives wrapped in a one-element list.
        aplanadas.extend(diferencia if isinstance(diferencia, list) else [diferencia])
    return aplanadas


def test_el_orm_y_las_migraciones_describen_el_mismo_esquema(motor_vacio):
    """La deriva de la auditoría, convertida en una prueba permanente.

    Eran tres columnas con ``DEFAULT`` en la migración y sin él en el ORM
    (``factor_tipo_vehiculo.factor_milesimas``,
    ``reserva_tarifa_desglose.factor_milesimas``, ``intento_login.exitoso``) y
    dos índices únicos que el ORM no declaraba
    (``ix_transaccion_pasarela_idempotency_key``,
    ``ix_reembolso_idempotency_key``). Ninguna rompía nada hoy, que es
    precisamente por qué llevaban ahí desde INC-3.

    A partir de ahora, cualquier columna, tipo, índice, clave foránea o
    ``DEFAULT`` que diverja rompe este test, y el mensaje dice exactamente
    cuál. **Sin excepciones ni normalizaciones**: la lista tiene que estar
    vacía. Un filtro «para el ruido conocido» es exactamente por donde vuelve
    a entrar la deriva.
    """
    _correr(motor_vacio, "head")

    assert _diferencias(motor_vacio) == []


# --------------------------------------------------------------------------
# 0012 - lo que una instalación existente recibe
# --------------------------------------------------------------------------
def test_la_migracion_0012_concede_disponibilidad_al_mostrador(motor_vacio):
    """RF-015: el recepcionista tiene que poder ver los bloques libres.

    ``app/seed.py`` se lo da a una instalación nueva; esta migración se lo da
    a las que ya existen, que es el caso que el seed NO cubre: sembrar otra
    vez no es algo que nadie haga al actualizar.

    El escenario está montado como pasa de verdad: se sube a ``0011``, se
    siembra, se quita la concesión para simular la instalación antigua, y se
    sube a ``0012``.
    """
    from sqlalchemy.orm import sessionmaker

    from app.seed import ejecutar_seed

    _correr(motor_vacio, "0011")
    sesion = sessionmaker(bind=motor_vacio)()
    ejecutar_seed(sesion)
    sesion.close()

    borrar = text(
        "DELETE FROM rol_permiso WHERE rol_id IN "
        "(SELECT id FROM rol WHERE nombre = 'recepcionista') AND permiso_id IN "
        "(SELECT id FROM permiso WHERE codigo = 'disponibilidad:leer')"
    )
    with motor_vacio.begin() as conexion:
        conexion.execute(borrar)

    assert not _mostrador_puede_ver_disponibilidad(motor_vacio), "instalación antigua simulada"

    _correr(motor_vacio, "0012")

    assert _mostrador_puede_ver_disponibilidad(motor_vacio)

    _correr(motor_vacio, "0011", desde="0012")

    assert not _mostrador_puede_ver_disponibilidad(motor_vacio), "el downgrade la retira"


def _mostrador_puede_ver_disponibilidad(motor) -> bool:
    from sqlalchemy.orm import sessionmaker

    sesion = sessionmaker(bind=motor)()
    try:
        rol = sesion.scalars(select(Rol).where(Rol.nombre == "recepcionista")).first()
        permiso = sesion.scalars(
            select(Permiso).where(Permiso.codigo == "disponibilidad:leer")
        ).first()
        assert rol is not None and permiso is not None
        return permiso in rol.permisos
    finally:
        sesion.close()
