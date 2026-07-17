# Mejoras pendientes — CopyMT5

Checklist de optimizaciones aún no implementadas.
Actualizar este archivo al completar cada ítem.

---

## Hecho

- [x] **Fan-out paralelo** — 1 proceso por follower + sesión MT5 caliente (`core/worker.py`)
- [x] **Master sesión caliente + menos idle** — connector persistente, balance en el mismo snapshot, `fallback_interval` 30s
- [x] **ACK formal + retry** — `CopyAck` por worker, `SyncCoordinator`, open idempotente, persistencia en `state/pending_events.json` (max 5 intentos, backoff, timeout 20s)

---

## Pendiente

### P0 — Confiabilidad

#### 1. ~~Sync con ACK + retry~~ ✅ hecho

---

#### 1b. Catch-up / reconciliación de posiciones abiertas

**Problema:** al arrancar, `initialize_snapshot()` absorbe las posiciones ya abiertas del master **sin copiarlas**. Si el copier estuvo caído (UTM bloqueado, crash, reinicio) mientras el master abría, esas posiciones quedan huérfanas: el master las tiene, los followers no, y el loop normal nunca las ve como “nuevas”.

**Diferencia con lo ya implementado:**

| Mecanismo | Cuándo | Qué cubre |
|---|---|---|
| `load_and_resume()` ✅ | Al arrancar | Eventos ya detectados que quedaron pendientes de ACK en `state/pending_events.json` |
| **Catch-up** (nuevo) | Al arrancar / periódico | Posiciones que el copier **nunca llegó a ver** porque estaba caído |

**Propuesta:** comparar master vs cada follower usando el tag `copy#{master_ticket}`:

```
faltantes  = master abierto sin copy#ticket en follower  → ABRIR (o reportar)
sobrantes  = copy#ticket en follower cuyo master ya no existe → CERRAR (solo en modo full)
desajustadas = SL/TP distintos (opcional) → MODIFICAR
```

Encaje en `main.py` (después de snapshot, antes del loop):

```
coordinator.load_and_resume()
monitor.initialize_snapshot()
coordinator.catch_up(master_positions)   # ← nuevo
monitor.start()
```

**Modos** (de menos a más agresivo):

| Modo | Comportamiento | Riesgo |
|---|---|---|
| `off` (actual) | Ignora abiertas al arrancar | Followers desincronizados tras caída |
| `report` | Solo loguea diferencias, no opera | Cero; decisión manual |
| `open_missing` | Abre en followers lo que falta del master | Medio: precio de entrada distinto al original |
| `full` | Abre faltantes y cierra sobrantes | Alto si el mapeo por comment falla |

**Punto delicado — precio de entrada:** el catch-up copia la *posición*, no el precio histórico. Si el master abrió hace horas y el mercado se movió, el follower entra al precio actual. Por eso hace falta una ventana de tolerancia:

```json
"catch_up": {
  "mode": "report",
  "max_age_minutes": 10,
  "max_price_drift_pct": 0.3,
  "interval_minutes": 0
}
```

- Si la posición es más vieja que `max_age_minutes` o el drift de precio supera el umbral → solo reportar.
- `interval_minutes > 0` → también correr catch-up periódico como auto-heal (además del arranque).

**Fases sugeridas:**

1. Modo `report` (log + conteo de faltantes/sobrantes)
2. Modo `open_missing` con ventana de edad/drift + open idempotente (ya existe)
3. Opcional: periódico + modo `full`

**Contexto:** incidente 2026-07-16 — UTM bloqueó la VM; al reiniciar el copy volvió a funcionar pero las órdenes ya abiertas del master no se copiaron (comportamiento esperado del snapshot). El catch-up cubre exactamente ese hueco.

---



### P0 — Riesgo / sizing



#### 2. Política de lotes alineada al master

**Problema:** con `multiplier` + `max_lot`, gran parte de los índices follower terminan en el tope; se pierde correlación de riesgo. Símbolos sin regla (B/C 300, BullX500) copian 1:1.

**Opciones (elegir una):**

- **A.** Mapa explícito `copy_lots` (recomendado para control fino)
- **B.** Multiplier más bajo o `lot_mode: proportional` + `max_lot` por capital
- **C.** Overrides `copy_settings` por follower (Nico ≠ Adamo ≠ Mari)
- **D.** Reglas o blacklist para B/C 300 y BullX500

**Nota:** `volatility_lot_boost` no aplica a símbolos con regla (retorno temprano en `_calc_lot`). `lot_ranges` queda inactivo si `recalculate_lot: false` y hay reglas de símbolo.

---

#### 2b. Tope de lote automático según balance del follower (`capital_lot`)

**Problema:** hoy el `max_lot` es fijo en config. Cuentas de ~200 vs ~300 no deberían arriesgar igual; el master abre ≥4 concurrentes con frecuencia (~1 cada 1.3 días en el histórico). Lote mínimo broker en estos índices: **0.2**.

**Propuesta:** antes de cada `order_send` (open), leer el **balance real** del follower (no equity/crédito/bono) y aplicar un tope dinámico:

```
lot = _calc_lot(...)                         # multiplier / copy_lots / max_lot símbolo
lot = min(lot, resolve_max_lot(balance))     # tope por capital
lot = max(lot, volume_min)                   # piso 0.2
```

**Tramos sugeridos** (balance real; bono solo para margen):

| Balance | `max_lot` |
|---|---|
| 0 – 249 | 0.20 |
| 250 – 299 | 0.30 |
| 300 – 399 | 0.50 |
| 400+ | 0.50 |

**Config ejemplo:**

```json
"capital_lot": {
  "enabled": true,
  "use": "balance",
  "absolute_max_lot": 0.5,
  "tiers": [
    { "from": 0, "to": 249, "max_lot": 0.20 },
    { "from": 250, "to": 299, "max_lot": 0.30 },
    { "from": 300, "to": 399, "max_lot": 0.50 },
    { "from": 400, "to": 999999, "max_lot": 0.50 }
  ]
}
```

**Implementación:**

- Parse en `config/settings.py`; aplicar en `TradeCopier.open_position` (después de `_calc_lot`)
- Usar **`balance`**, no equity+credit (el bono no debe inflar el riesgo)
- `absolute_max_lot` como techo duro; el `max_lot` del símbolo puede seguir como techo adicional
- Log: `balance → max_lot_tier → lot final`
- Opcional: skip/ACK fail si `free_margin` insuficiente
- Redondear a `volume_step` del símbolo

**Contexto de decisión (histórico master 911217):** 2 cuentas de 300 @ 0.5 es más viable que 3 de 200 @ 0.4 a igual capital total; con min 0.2, en cuentas chicas el tope dinámico es la palanca principal.

---



### P1 — Producto del copy



#### 3. Huecos de sync de posiciones


| Hueco                             | Riesgo                                                                           |
| --------------------------------- | -------------------------------------------------------------------------------- |
| Partial close / cambio de volumen | Se detecta como `modified` pero solo se copia SL/TP → volumen follower desfasado |
| Pending orders                    | No se copian                                                                     |
| Comment truncado por broker       | Close/modify no encuentra `copy#…`                                               |
| Mapa solo por comment             | Frágil                                                                           |


**Propuesta:** `ticket_map` persistente (JSON/SQLite) + handler de cambio de volumen / partial close.

---



### P2 — Observabilidad



#### 4. Métricas mínimas

Sin métricas se optimiza a ciegas. Añadir (log estructurado o archivo):

- Latencia señal → fill por follower
- Tasa de éxito open/close
- Contadores miss / retry
- Lot master → lot follower aplicado
- Tiempo de reconnect

---



## No priorizar por ahora

- Bajar `poll_interval` a 0.1s (con EA + holds ~25 min gana poco)
- Complejidad de `volatility_lot_boost` mientras el `max_lot` aplaste el sizing
- Optimizar `lot_ranges` con el setup actual de reglas por símbolo

---



## Orden sugerido de implementación

1. ~~Sync ACK + retry + open idempotente~~ ✅
2. **Tope de lote por balance (`capital_lot`)** — tramos 200→0.2 / 300→0.5
3. **Catch-up** — empezar en modo `report`, luego `open_missing` con ventana de edad/drift
4. Política de lotes fina (`copy_lots` / overrides + símbolos huérfanos) si aún hace falta
5. Ticket map + partial close
6. Métricas de latencia / éxito

