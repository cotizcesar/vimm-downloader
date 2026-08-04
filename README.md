# Vimm's Lair Vault Downloader

Descarga en lote todos los juegos de una o varias consolas de **The Vault**
(https://vimm.net/vault).

Compatibilidad: funciona con cualquier sistema del Vault (SNES, NES, N64, GB,
GBA, Genesis, PS1, PSP, ...). Cada consola se identifica por su código, por
ejemplo `SNES`, `NES`, `N64`.

## Características

- **Reintentos** con backoff exponencial ante fallos de red o respuestas HTTP.
- **Throttle** configurable (segundos de espera entre cada petición HTTP).
- **Descargas reanudables** (HTTP Range) y **omisión de archivos ya descargados**.
- **Estado persistente** en un archivo JSON para reanudar corridas interrumpidas.
- Detecta y descarta páginas HTML de bloqueo para re-descargarlas después.
- Descarga la versión por defecto de cada juego, o **todas las versiones/discos**
  con `--all-versions`.

## Requisitos

- Python 3.8+
- Biblioteca `requests`:

```bash
pip install requests
```

## Uso

```bash
# Una consola (todas las letras A-Z y la sección numérica)
python3 vimm_downloader.py --systems SNES --output ./roms

# Varias consolas
python3 vimm_downloader.py --systems SNES,NES,N64 --output ./roms

# Con throttle personalizado y más reintentos
python3 vimm_downloader.py --systems SNES --output ./roms --throttle 2 --retries 5

# Solo una sección, o todas las versiones por juego
python3 vimm_downloader.py --systems N64 --letter A --output ./roms
python3 vimm_downloader.py --systems SNES --all-versions --output ./roms
```

## Opciones

| Opción | Descripción | Default |
| ------ | ----------- | ------- |
| `--systems` | Códigos de consola separados por coma (obligatorio). Ej.: `SNES,NES,N64` | — |
| `--output` | Carpeta de salida. | `./roms` |
| `--throttle` | Segundos de espera entre peticiones HTTP. | `1.5` |
| `--retries` | Máximo de reintentos por petición. | `5` |
| `--backoff` | Segundos base del backoff (se duplica por reintento). | `3` |
| `--timeout` | Timeout por petición en segundos. | `30` |
| `--letter` | Solo procesar secciones indicadas, ej. `A,C,#`. | todas |
| `--all-versions` | Descargar todas las versiones/discos de cada juego. | no |
| `--no-resume` | No reanudar descargas parciales (reiniciarlas). | reanuda |
| `--always-download` | Re-descargar aunque el archivo ya exista. | omite |
| `--state` | Ruta del archivo de estado. | `./.vimm_downloader_state.json` |
| `--log-level` | `DEBUG`, `INFO`, `WARNING` o `ERROR`. | `INFO` |

## Salida

Los archivos se guardan por consola y por juego:

```
roms/
├── SNES/
│   ├── 1004/
│   │   └── Aaahh!!! Real Monsters (USA).zip
│   ├── 1386/
│   │   └── Mega Man X (USA).zip
│   └── ...
├── NES/
└── N64/
```

> El script usa el nombre de archivo que entrega el servidor (`Content-Disposition`).

## Reanudar una descarga interrumpida

Simplemente vuelve a ejecutar el mismo comando. Los juegos ya descargados se
omiten y las descargas parciales se retoman desde donde quedaron:

```bash
python3 vimm_downloader.py --systems SNES,NES,N64 --output ./roms
```

## Nota legal

Vimm's Lair aloja ROMs de juegos con derechos de autor. Antes de descargar,
verifica las leyes de tu país y que cuentes con los permisos correspondientes.
Úsalo con responsabilidad: respeta la velocidad y el throttle para no saturar el
servidor.
