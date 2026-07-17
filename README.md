# CopyMT5

Replicador de operaciones entre cuentas MetaTrader 5. Monitorea una cuenta **master** y copia cada operación (apertura, cierre, modificación de SL/TP) en una o más cuentas **follower** en tiempo real.

> **Requisito de plataforma:** Solo funciona en **Windows**. La librería oficial `MetaTrader5` de Python usa la interfaz COM del terminal y no está disponible en Linux ni macOS.

---

## Características

- Detecta aperturas, cierres y modificaciones de posiciones en la cuenta master
- Replica automáticamente en todas las cuentas follower configuradas
- **Fan-out paralelo**: un proceso por follower con sesión MT5 caliente (sin reconnect por orden)
- **ACK + retry**: cada worker confirma ok/fail; reintentos con backoff; open idempotente (`copy#{ticket}`); pendientes en `state/pending_events.json`
- Funciona sin importar desde dónde se origine la operación: desktop, celular, web
- Monitor basado en eventos (EA + watchdog) — Python solo despierta cuando hay actividad
- Fallback automático a polling si el EA no está instalado
- Cálculo de lote flexible: copia exacto, fijo, multiplicador, proporcional al balance o por rangos
- Al arrancar toma un snapshot inicial para no replicar posiciones ya abiertas
- Logs en consola y en archivo rotativo diario (`logs/`)
- Reconexión automática con reintentos ante fallos de conexión
- Configuración centralizada en un solo archivo `config.json`

---

## Requisitos

- Windows 10 / 11 (o VM Windows)
- Python 3.11 64-bit
- Una instalación de MT5 por cada cuenta (master + followers)

---

## Instalación

### 1. Clonar / copiar el proyecto en Windows

```
C:\Projects\copy_mt5\
```

### 2. Instalar dependencias

```powershell
cd C:\Projects\copy_mt5
pip install -r requirements.txt
```

### 3. Instalar los terminales MT5

Cada cuenta necesita su propio terminal en una carpeta separada. La forma más sencilla es instalar MT5 una vez y luego copiar la carpeta:

```powershell
# Instalar la primera vez en:
C:\MT5\Master\

# Copiar para cada follower
xcopy C:\MT5\Master C:\MT5\Follower1 /E /I /Y
xcopy C:\MT5\Master C:\MT5\Follower2 /E /I /Y
```

Abrir cada `terminal64.exe`, iniciar sesión con la cuenta correspondiente y activar:

> **Tools → Options → Expert Advisors**
> - ✅ Allow automated trading
> - ✅ Allow DLL imports

Todos los terminales deben estar **abiertos y con sesión activa** mientras el script está corriendo.

### 4. Instalar el EA TradeSignaler en el terminal master

El EA es el componente que avisa a Python cuando ocurre cualquier evento de trading.

1. Copia `ea/TradeSignaler.mq5` a la carpeta de EAs del terminal master:
   ```
   C:\MT5\Master\MQL5\Experts\TradeSignaler.mq5
   ```
2. En el terminal master: **Tools → MetaEditor** (o F4), compila el archivo (F7).
3. Arrastra el EA `TradeSignaler` desde el panel Navigator a cualquier gráfico abierto.
4. Asegúrate de que el botón **AutoTrading** de la barra de herramientas esté activo (ícono verde).

> El EA no abre ni cierra ninguna operación. Solo escribe un archivo de señal cada vez que `OnTrade()` se dispara.

### 5. Crear el archivo de configuración

```powershell
copy config.json.example config.json
notepad config.json
```

Completar con los datos reales (ver sección [Configuración](#configuración)).

### 6. Ejecutar

```powershell
python main.py
```

Salida esperada con el EA instalado:

```
INFO  | Iniciando CopyMT5 | Master: Master | Followers: 4
INFO  | [FANOUT] Worker 'Nico Follower 1' PID=...
INFO  | [FANOUT] 4 follower(s) en paralelo (sesión caliente)
INFO  | [MASTER] Snapshot inicial: 0 posición(es) abiertas.
INFO  | Monitor modo EVENTO activo.
INFO  | Modo: EVENTO (watchdog) | Fallback seguridad cada 30.0s | master sesión caliente
INFO  | Monitoreo activo | fan-out paralelo=4 workers. Presiona Ctrl+C para detener.
```

---

## Cómo funciona el monitor

```
Celular / Web / Desktop
        │
        ▼
   Broker Server
        │  sincroniza
        ▼
Terminal MT5 Master (Windows)
        │  OnTrade() se dispara
        ▼
  EA TradeSignaler
        │  escribe copy_mt5_signal.txt
        ▼
  watchdog (Python)          ← despierta solo cuando hay actividad
        │
        ▼
  positions_get()  →  detecta cambios
        │
        ▼
  Fan-out paralelo (1 proceso / follower, sesión MT5 caliente)
        ├── Worker Follower 1 → order_send → ACK
        ├── Worker Follower 2 → order_send → ACK
        └── Worker Follower N → order_send → ACK
                │
                ▼
        SyncCoordinator (retry / timeout / persistencia)
```

Python **no pregunta en loop**. El EA escribe el archivo al instante, watchdog usa la API nativa de Windows (`ReadDirectoryChangesW`) para detectar el cambio, y Python reacciona de inmediato.

Cada follower corre en su **propio proceso** con la conexión MT5 abierta de forma persistente: al llegar una señal, todos copian en paralelo (sin `initialize`/`shutdown` por cada orden). El terminal **master** también mantiene sesión caliente: el balance se lee en el mismo snapshot, sin reconnect extra.

Tras cada open/close/modify el worker envía un **ACK** al proceso principal. Si falla de forma recuperable (connect, timeout, requote…), se reintenta con backoff (hasta 5 intentos). El open es **idempotente**: si ya existe `copy#{master_ticket}`, no reabre. Los eventos pendientes se guardan en `state/pending_events.json` y se reanudan al reiniciar.

Si el EA no está instalado o el archivo no cambia, el sistema tiene un **fallback** que verifica cada `fallback_interval` segundos (30s por defecto) para no perder eventos.

---

## Configuración

El archivo `config.json` tiene tres secciones. **No se sube al repositorio** (está en `.gitignore`). Usar `config.json.example` como plantilla.

### master

La cuenta que se monitorea.

| Campo | Tipo | Descripción |
|---|---|---|
| `login` | número | Número de cuenta MT5 |
| `password` | string | Contraseña de la cuenta |
| `server` | string | Nombre del servidor del broker |
| `path` | string | Ruta al `terminal64.exe` del terminal master |
| `label` | string | Nombre descriptivo (solo para logs) |

```json
"master": {
  "login": 12345678,
  "password": "tu_password",
  "server": "NombreBroker-Real",
  "path": "C:\\MT5\\Master\\terminal64.exe",
  "label": "Master"
}
```

### followers

Lista de cuentas que recibirán las operaciones. Para agregar más cuentas en el futuro, añadir un objeto más al array.

```json
"followers": [
  {
    "login": 87654321,
    "password": "password_follower1",
    "server": "NombreBroker-Real",
    "path": "C:\\MT5\\Follower1\\terminal64.exe",
    "label": "Follower 1"
  }
]
```

### copy_settings

| Campo | Tipo | Descripción |
|---|---|---|
| `recalculate_lot` | bool | `false` = copia el lote del master exacto. `true` = aplica `lot_mode`. |
| `lot_mode` | string | `"range"` \| `"fixed"` \| `"multiplier"` \| `"proportional"` |
| `lot_value` | número | Valor base para `fixed` y `multiplier`. Fallback de `range`. |
| `lot_ranges` | array | Tabla de rangos para `lot_mode: "range"` |
| `poll_interval` | número | Segundos entre consultas en modo polling (si watchdog no está disponible) |
| `fallback_interval` | número | Segundos máximos de espera en modo evento antes de verificar de todas formas (default: `30.0`) |
| `max_slippage` | número | Slippage máximo en puntos al enviar órdenes |

#### Modos de lote

| `recalculate_lot` | `lot_mode` | Comportamiento |
|---|---|---|
| `false` | cualquiera | Copia el lote del master sin modificar |
| `true` | `"range"` | Mapea el lote del master al rango correspondiente |
| `true` | `"multiplier"` | Lote follower = lote master × `lot_value` |
| `true` | `"proportional"` | Escalado según balance follower / master |
| `true` | `"fixed"` | Siempre usa `lot_value` fijo |

#### Rangos de lote

```json
"copy_settings": {
  "recalculate_lot": true,
  "lot_mode": "range",
  "lot_value": 0.01,
  "lot_ranges": [
    { "from": 0.01, "to": 0.05, "lot": 0.01 },
    { "from": 0.06, "to": 0.10, "lot": 0.02 },
    { "from": 0.11, "to": 0.50, "lot": 0.05 },
    { "from": 0.51, "to": 99.0, "lot": 0.10 }
  ],
  "poll_interval": 0.5,
  "fallback_interval": 30.0,
  "max_slippage": 10
}
```

Ejemplo: master abre con `0.08` → follower abre con `0.02`.

---

## Estructura del proyecto

```
copy_mt5/
├── main.py                  # Entry point
├── config.json.example      # Plantilla de configuración (se sube al repo)
├── config.json              # Configuración real (NO se sube al repo)
├── requirements.txt
├── ea/
│   └── TradeSignaler.mq5    # EA para el terminal master (notificaciones de eventos)
├── config/
│   └── settings.py          # Carga y valida config.json
├── core/
│   ├── connector.py         # Conexión/desconexión a terminales MT5
│   ├── monitor.py           # Monitor basado en eventos (watchdog + EA)
│   └── copier.py            # Replica operaciones en los followers
├── models/
│   └── position.py          # Modelo de datos de una posición abierta
└── utils/
    └── logger.py            # Logs en consola + archivo rotativo
```

---

## Logs

Los logs se escriben en consola y en archivos diarios dentro de `logs/`, rotando a medianoche y conservando 30 días.

```
2026-06-18 10:05:12 | INFO    | [MASTER] 1 nueva(s): ['EURUSD']
2026-06-18 10:05:12 | SUCCESS | [FOLLOWER Follower 1] Posición abierta: EURUSD BUY 0.02 lotes (master=0.08) | ticket=12345
```

---

## Ejecutar como servicio Windows (opcional)

```powershell
# Descargar nssm.exe — nssm.cc
nssm install CopyMT5 C:\Python311\python.exe C:\Projects\copy_mt5\main.py
nssm set CopyMT5 AppDirectory C:\Projects\copy_mt5
nssm start CopyMT5
```

---

## Solución de problemas

| Error / Síntoma | Causa | Solución |
|---|---|---|
| `No se encontró 'config.json'` | Falta el archivo de configuración | Copiar `config.json.example` → `config.json` y completar |
| `IPC timeout` | Terminal MT5 no abierto | Abrir el terminal correspondiente |
| `TRADE_RETCODE_TRADE_DISABLED` | AutoTrading desactivado | Tools → Options → Expert Advisors → Allow automated trading |
| `Invalid volume` | Lote fuera del rango del broker | Ajustar rangos o `lot_value` al mínimo del broker |
| `ModuleNotFoundError: MetaTrader5` | Python 32-bit o librería no instalada | Usar Python 3.11 **64-bit** y `pip install MetaTrader5` |
| Monitor en modo POLLING en lugar de EVENTO | `watchdog` no instalado o EA no adjunto | `pip install watchdog` y adjuntar `TradeSignaler` al gráfico |
| EA no compila | Carpeta incorrecta | Verificar que el `.mq5` esté en `MQL5\Experts\` del terminal master |
