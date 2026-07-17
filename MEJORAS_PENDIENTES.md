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

Opcional futuro: reconciliación periódica vs deals del master (catch-up histórico).

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
3. Política de lotes fina (`copy_lots` / overrides + símbolos huérfanos) si aún hace falta
4. Ticket map + partial close
5. Métricas de latencia / éxito

