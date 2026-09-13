# Vimm's Lair Vault Downloader

Descarga en lote todos los juegos de una o varias consolas de **The Vault**
(https://vimm.net/vault).

Compatibilidad: funciona con cualquier sistema del Vault (SNES, NES, N64, GB,
GBA, Genesis, PS1, PSP, ...). Cada consola se identifica por su código, por
ejemplo `SNES`, `NES`, `N64`.

## Características

- **Reintentos** con backoff exponencial ante fallos de red, HTTP 403/429/503 y bloqueos Cloudflare/Turnstile.
- **Bypass Turnstile**: detecta `cf-turnstile` en `vault/<id>` y hace fallback a descarga directa `dl3/?mediaId` con título real del listado.
- **Fallback de hosts** de descarga (`dl3` → `download`) con cache y manejo de `429` rate-limit.
- **Barra de progreso por archivo** con velocidad `MB/s` y `ETA` (`tqdm` si está instalado, si no barra manual `█░`).
- **Logs silenciosos**: en `INFO` solo `→ Descargando` / `✓ Descargado` / `↷ Omitido` y `… MB/s` — sin ruido de `404`/`HEAD`.
- **Throttle** configurable (segundos entre peticiones) para evitar `429`.
- **Descargas reanudables** (`HTTP Range`) y **omisión** de archivos ya descargados.
- **Estado persistente** en JSON para reanudar corridas interrumpidas (`Ctrl-C` y relanza).
- Detecta y descarta páginas HTML de bloqueo para re-descargarlas después.
- Descarga la versión por defecto, o **todas las versiones/discos** con `--all-versions`.
- Modo **`--dry-run`** para previsualizar sin descargar.
- **Paginación** automática.

## Requisitos

- Python 3.8+
- `requests` y `tqdm` (opcional pero recomendado para barra rica):

```bash
pip install -r requirements.txt
# o
pip install requests tqdm
# o instalación editable
pip install -e .
```

## Uso

El comando más simple por defecto (usa `throttle 1.5`, `INFO` con barra):

```bash
python3 vimm_downloader.py --systems GB --output ./roms
```

Más ejemplos:

```bash
# Varias consolas
python3 vimm_downloader.py --systems SNES,NES,N64 --output ./roms

# Previsualizar sin descargar
python3 vimm_downloader.py --systems SNES --dry-run --output ./roms

# Más rápido (bajo tu riesgo de 429/ban)
python3 vimm_downloader.py --systems SNES --output ./roms --throttle 0.8

# Solo una sección, o todas las versiones por juego
python3 vimm_downloader.py --systems N64 --letter A --output ./roms
python3 vimm_downloader.py --systems SNES --all-versions --output ./roms

# Binario instalado
vimm-downloader --systems SNES --output ./roms
```

Logs con `INFO` (por defecto):
```
→ Descargando [3086] Game & Watch Gallery (USA).zip
  [█████████████░░░░░░░░░░░░]  62.5%  150/240 MB 1.2 MB/s ETA 01:12 Game & Watch Gallery
✓ Descargado [3086] Game & Watch Gallery (USA).zip (117359 bytes)
↷ Omitido [2941] ya existe: College Slam (USA).zip
Listo: 12 descargados, 180 omitidos, 2 fallidos
```
Con `DEBUG` ves `Turnstile`, `HEAD 429`, `retry`.

## Opciones

| Opción | Descripción | Default |
| ------ | ----------- | ------- |
| `--systems` | Códigos de consola separados por coma (obligatorio). Ej.: `SNES,NES,N64` | — |
| `--output` | Carpeta de salida. | `./roms` |
| `--throttle` | Segundos entre peticiones HTTP (evita 429). | `1.5` |
| `--retries` | Máximo de reintentos por petición. | `5` |
| `--backoff` | Segundos base del backoff (se duplica por reintento, `429` usa 10s). | `3` |
| `--timeout` | Timeout por petición en segundos. | `30` |
| `--letter` | Solo secciones indicadas, ej. `A,C,#`. | todas |
| `--all-versions` | Descargar todas las versiones/discos. | no |
| `--no-resume` | No reanudar parciales (reiniciar). | reanuda |
| `--always-download` | Re-descargar aunque exista. | omite |
| `--state` | Ruta del estado. | `./.vimm_downloader_state.json` |
| `--log-level` | `DEBUG`, `INFO`, `WARNING` o `ERROR`. | `INFO` |
| `--dry-run` | Solo listar lo que se descargaría. | no |
| `--dl-host` | Override host descarga. | `https://dl3.vimm.net` |

## Salida

Directo en carpeta por consola (sin subcarpetas por juego):

```
roms/
├── SNES/
│   ├── Aaahh!!! Real Monsters (USA).zip
│   ├── Mega Man X (USA).zip
│   └── ...
├── NES/
└── N64/
```

> Usa `Content-Disposition` del servidor. Si dos juegos comparten nombre, añade `ID`.

## Reanudar

`Ctrl-C` y relanza el mismo comando. Omitidos se marcan `↷ Omitido`, parciales se retoman:

```bash
python3 vimm_downloader.py --systems SNES,NES,N64 --output ./roms
```

## Reparar layout antiguo

Si usaste versión que guardaba `roms/SNES/1004/Game.zip`:

```bash
python3 flatten_roms.py --input ./roms        # mueve y borra vacías
python3 flatten_roms.py --input ./roms --dry-run
```

Borra sintéticos viejos `Game 1234.zip` si los tienes (bug de título antes de `95cab7a`):
```bash
rm "roms/GB/Game 3057.zip" "roms/GB/Game 45944.zip"
# y limpia estado: python3 -c "import json,pathlib; p=pathlib.Path('.vimm_downloader_state.json'); d=json.loads(p.read_text()); d.pop('3057',None); p.write_text(json.dumps(d,indent=2,sort_keys=True))"
```

## Desarrollo

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -v
python3 -m py_compile vimm_downloader.py flatten_roms.py
```

## Nota legal

ROMs con derechos. Verifica leyes locales y que tengas licencia. Respeta `throttle` para no saturar el servidor.
