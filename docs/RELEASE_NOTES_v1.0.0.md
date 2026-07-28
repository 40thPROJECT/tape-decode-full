Decode one tape across **several machines**. Adds `split`, `merge` and `insert`
to [tape-decode-rust](https://github.com/harrypm/tape-decode-rust), in the CLI
and in the launcher.

Decodifica una cinta repartiéndola entre **varias máquinas**. Añade `split`,
`merge` e `insert` a [tape-decode-rust](https://github.com/harrypm/tape-decode-rust),
tanto por línea de comandos como en el lanzador.

## Downloads / Descargas

| file | | |
|---|---|---|
| `tape-decode-full-gui-windows_1.0.0_x86_64.exe` | 105 MB | GUI, CLI bundled inside · interfaz, con el CLI dentro |
| `tape-decode-full-cli-windows_1.0.0_x86_64.exe` | 59 MB | command line only · solo línea de comandos |
| `SHA256SUMS.txt` | | verify / verificación |

Windows x86-64. Built with the `x86_64-pc-windows-gnu` toolchain rather than
MSVC. Compilados con el toolchain `x86_64-pc-windows-gnu`, no con MSVC.

## What's in it

**`split`** — cuts a capture into standalone pieces. A byte copy: nothing is
re-encoded and the pieces are bit-identical to the corresponding samples. Reads
the capture's length from its own RF Vorbis tags or from the capture tool's
`.json` sidecar.

**`merge`** — joins the decoded `.tbc` files, trimming the overlap, repairing
field parity at every join, and refusing files given out of tape order.

**`insert`** — fills a gap left by a piece that had to be decoded again. Works in
place, so the only extra space needed is the size of the insert.

Guided forms for all three in the launcher, with validation that blocks launching
while anything is wrong.

## Qué trae

**`split`** — corta una captura en piezas autónomas. Es una copia de bytes: no se
recodifica nada y las piezas son idénticas bit a bit a las muestras
correspondientes. Lee la duración de las etiquetas Vorbis de la propia captura o
del `.json` de la herramienta de captura.

**`merge`** — une los `.tbc` decodificados, recorta el solape, repara la paridad
de campo en cada unión, y se niega si le pasas los ficheros desordenados.

**`insert`** — rellena el hueco que deja una pieza que hubo que decodificar otra
vez. Trabaja en el propio fichero, así que solo necesita el espacio del trozo
insertado.

Formularios para las tres en el lanzador, con validaciones que impiden lanzar
mientras haya algo mal.

## Quick start / Para empezar

```bash
tape-decode-full split capture.ldf pieces/ --parts 4
tape-decode-full decode --profile NTSC_VHS --input-format flac pieces/capture.part00.ldf --output out
tape-decode-full merge pc1.tbc pc2.tbc pc3.tbc pc4.tbc -o tape -m pieces/capture.parts.json
```

Keep the `.parts.json`: without it `merge` cannot tell where each decode belongs.
Guarda el `.parts.json`: sin él `merge` no sabe dónde va cada decode.

## Before you reach for it / Antes de recurrir a esto

On a 6-core i5-11400F, decoding the same 60 s of a 40 MSPS NTSC capture, two runs
at each setting:

| `--mt-threads` | FPS | vs serial |
|---|---|---|
| 0 | 3.27 | 1.00x |
| 4 | 8.62 | 2.64x |
| 8 | 12.08 | 3.70x |
| 12 | 13.05 | 4.00x |

Threading alone is already 4x on one machine. Splitting across machines is for
when that is still not enough — and it is worth raising `--mt-threads` first.

Solo con los hilos ya tienes 4x en una única máquina. Repartir entre varias es
para cuando eso no basta; conviene subir `--mt-threads` antes que nada.

## Verified / Verificado

`split` was checked against a real capture at 3 and 5 pieces: every piece's
samples match the original exactly at the offset it declares. `merge` and
`insert` were checked on `.tbc` files where each field carries a pattern
identifying its position — right content, right order, no duplicates, parity
alternating, and both refusal paths. 11 unit tests.

`split` se comprobó contra una captura real con 3 y 5 piezas: las muestras de
cada pieza coinciden exactamente con el original en el offset que declara.
`merge` e `insert` se comprobaron sobre `.tbc` donde cada campo lleva un patrón
que identifica su posición. 11 tests unitarios.

Full documentation / Documentación completa:
**[README](https://github.com/40thPROJECT/tape-decode-full#readme)**

## Credits / Créditos

All the hard work is
[harrypm/tape-decode-rust](https://github.com/harrypm/tape-decode-rust),
[oyvindln/vhs-decode](https://github.com/oyvindln/vhs-decode) and
[harrypm/FLAC-Chop](https://github.com/harrypm/FLAC-Chop) — whose reading of the
RF Vorbis tags this borrows — and their contributors. This fork only makes it
finish sooner.

Todo el trabajo duro es de esos tres proyectos y de quienes contribuyen a ellos.
Este fork solo hace que termine antes.

**by ElMamadoJoe**
