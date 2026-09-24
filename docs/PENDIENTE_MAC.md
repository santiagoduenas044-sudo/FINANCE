# Pendiente en la Mac

Copia y pega **un bloque a la vez**, en orden, y espera a que termine cada uno.
Todo se corre en la Terminal.

---

## 1. Actualizar el bot (2 min)

```bash
cd ~/FINANCE
```

```bash
git pull
```

```bash
bash deploy/macos/ctl.sh restart
```

Revisa Discord: debe llegar el mensaje de arranque con **"strategy: zones"**.
También puedes escribir `!status` en Discord.

Si no llega nada en 1–2 minutos, corre esto, espera unos segundos, sal con
`Ctrl+C` y manda captura:

```bash
bash deploy/macos/ctl.sh logs
```

---

## 1b. Corregir la IDEA #4 (hacerlo en los próximos días: Yahoo guarda ~7 días de 1m)

Instalar lo necesario para la gráfica:

```bash
.venv/bin/pip install -r requirements.txt
```

Ver el resultado corregido (no cambia nada todavía):

```bash
PYTHONPATH=. .venv/bin/python scripts/correct_trade.py --idea 4
```

Si el "corregido" se ve bien, aplicarlo y mandarlo a Discord con la gráfica:

```bash
bash deploy/macos/ctl.sh stop
```

```bash
PYTHONPATH=. .venv/bin/python scripts/correct_trade.py --idea 4 --offline --apply --discord
```

```bash
bash deploy/macos/ctl.sh start
```

---

## 2. Prueba con datos reales de Yahoo (~30 min, puede ser en la noche)

Descarga ~29 días de NQ a 1 minuto y los pasa por la estrategia. El bot puede
seguir encendido mientras tanto.

```bash
PYTHONPATH=. .venv/bin/python scripts/audit_real_data.py
```

**Mandar:** todo lo que imprima desde `=== AUDITORÍA` hasta el final.

---

## 3. Datos históricos de Databento (5 años)

**3.1** En databento.com: Menú (☰) → **API Keys** → copia la clave `db-...`.
No la mandes por el chat.

**3.2** Instalar el cliente:

```bash
.venv/bin/pip install databento
```

**3.3** Guardar la clave en `.env`:

```bash
open -e .env
```

Agrega esta línea con tu clave, guarda y cierra:

```
DATABENTO_API_KEY=db-tuclaveaqui
```

**3.4** Descargar. Primero muestra el costo y pregunta; se espera ~$7, pagado
con el crédito gratis. Si sale muy distinto, responde `n` y manda captura.

```bash
PYTHONPATH=. .venv/bin/python scripts/fetch_databento.py --start 2021-09-24
```

**3.5** Probar con un mes (~1 hora):

```bash
PYTHONPATH=. .venv/bin/python scripts/audit_real_data.py --offline --contract DATABENTO --start 2026-08-01 --end 2026-08-31
```

**Mandar:** todo lo que imprima desde `=== AUDITORÍA` hasta el final.

Si ese mes sale bien, después corremos tramos más largos (un trimestre por
noche, por ejemplo).

---

## Qué NO hacer

- No uses el botón "Submit request" de la página de Databento: mezcla todos
  los contratos y no tiene reembolso. Usa el script del paso 3.4.
- No pegues claves (Databento, Discord) en el chat, solo en `.env`.

## Pendiente del lado de Claude

- Versión de la estrategia que confirma en 5 minutos (zonas en 15m), para
  comparar contra la de 1 minuto con los mismos datos de Databento.
