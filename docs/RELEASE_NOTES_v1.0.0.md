# v1.0.0 — split, merge and insert

**[English](#english) · [Español](#español)**

A fork of [harrypm/tape-decode-rust](https://github.com/harrypm/tape-decode-rust)
that adds decoding one tape across **several machines**, in the CLI and in the
launcher.

## Downloads

| file | what it is |
|---|---|
| `tape-decode-full-gui-windows_1.0.0_x86_64.exe` | GUI, with the CLI bundled inside |
| `tape-decode-full-cli-windows_1.0.0_x86_64.exe` | command line only |

Verify with `SHA256SUMS.txt`. Built with the `x86_64-pc-windows-gnu` toolchain
rather than MSVC.

---

## English

`--mt-threads` already spreads a decode across the cores of one machine, and it
does that well. This adds the other axis: cut the capture into standalone pieces,
decode a piece on each machine, and put the results back on one timeline.

### What's new

**`split`** cuts an RF capture into standalone pieces. FLAC frames are
self-contained, so a file made of the original headers plus a run of whole frames
is a valid capture covering that stretch of tape — splitting is a byte copy, with
no re-encoding, and the pieces are bit-identical to the corresponding samples.

Boundaries are found by bisecting on frame headers rather than by asking the
container where a timestamp lives. That matters on a long capture: past 2^36
samples a FLAC header cannot record its own length, and position estimates from
the bitrate can be far out — 25x short, on the 2h45m capture this was built for.

The length is read from the capture's own `RF_TOTAL_SAMPLES` Vorbis tag (MISRC)
or from the capture tool's `.json` sidecar (DomesDay Duplicator), and stated by
hand only when neither exists.

**`merge`** joins the decoded `.tbc` files, trimming the overlap between pieces,
cutting each part where the next one actually started, skipping parts that
produced nothing, and repairing field parity at every join. It refuses files
given out of tape order rather than silently interleaving the tape.

**`insert`** fills a gap in the middle of a finished decode, for when one
machine's piece failed and had to be decoded again. It extends the file and
shifts the tail along instead of splitting and re-merging, so the only extra
space needed is the size of the insert.

The launcher has guided forms for all three, with validation that blocks
launching while anything is wrong.

### Measured

On an i5-11400F (6 cores, 12 threads), decoding the same 60 s of a 40 MSPS NTSC
VHS capture, two runs at each setting:

| `--mt-threads` | FPS | vs serial |
|---|---|---|
| 0 (serial) | 3.27 | 1.00x |
| 2 | 4.88 | 1.49x |
| 4 | 8.62 | 2.64x |
| 6 | 10.89 | 3.33x |
| 8 | 12.08 | 3.70x |
| 12 | 13.05 | 4.00x |

Worth knowing before reaching for `split`: threading alone already gives 4x on
one machine, so splitting across machines is for when that is still not enough.

`split` was verified against a real capture at 3 and 5 pieces — every piece's
samples match the original exactly at the offset it declares. `merge` and
`insert` were verified on constructed `.tbc` files where each field carries a
pattern identifying its position.

---

## Español

Un fork de [harrypm/tape-decode-rust](https://github.com/harrypm/tape-decode-rust)
que añade decodificar una cinta repartiéndola entre **varias máquinas**, tanto
por línea de comandos como en el lanzador.

### Descargas

| fichero | qué es |
|---|---|
| `tape-decode-full-gui-windows_1.0.0_x86_64.exe` | GUI, con el CLI incluido dentro |
| `tape-decode-full-cli-windows_1.0.0_x86_64.exe` | solo línea de comandos |

Verifícalos con `SHA256SUMS.txt`. Compilados con el toolchain
`x86_64-pc-windows-gnu`, no con MSVC.

### Qué añade

`--mt-threads` ya reparte un decode entre los núcleos de una máquina, y lo hace
bien. Esto añade el otro eje: cortar la captura en piezas autónomas, decodificar
una en cada máquina, y volver a juntar los resultados en una sola línea de
tiempo.

**`split`** corta una captura RF en piezas autónomas. Los frames FLAC son
autocontenidos, así que un fichero formado por las cabeceras originales más una
tirada de frames enteros es una captura válida de ese tramo de cinta — partir es
una copia de bytes, sin recodificar nada, y las piezas son idénticas bit a bit a
las muestras correspondientes.

Las fronteras se localizan bisecando sobre cabeceras de frame, no preguntándole
al contenedor dónde cae un timestamp. Eso importa en una captura larga: pasadas
2^36 muestras una cabecera FLAC no puede registrar su propia longitud, y las
estimaciones de posición a partir del bitrate pueden desviarse muchísimo — 25
veces corta, en la captura de 2 h 45 para la que se hizo esto.

La duración se lee de la etiqueta Vorbis `RF_TOTAL_SAMPLES` de la propia captura
(MISRC) o del `.json` que deja la herramienta de captura (DomesDay Duplicator), y
solo hay que indicarla a mano cuando no existe ninguna de las dos.

**`merge`** une los `.tbc` decodificados, recortando el solape entre piezas,
cortando cada parte donde empezó realmente la siguiente, saltando las que no
produjeron nada, y reparando la paridad de campo en cada unión. Se niega si le
pasas los ficheros desordenados, en vez de entrelazarte la cinta en silencio.

**`insert`** rellena un hueco en mitad de un decode ya terminado, para cuando la
pieza de una máquina falla y hay que decodificarla otra vez. Alarga el fichero y
desplaza la cola en vez de partirlo y volver a unirlo, así que el único espacio
extra necesario es el del trozo insertado.

El lanzador trae formularios para las tres, con validaciones que impiden lanzar
mientras haya algo mal.

### Medido

En un i5-11400F (6 núcleos, 12 hilos), decodificando los mismos 60 s de una
captura VHS NTSC a 40 MSPS, dos pasadas por cada valor:

| `--mt-threads` | FPS | vs serie |
|---|---|---|
| 0 (serie) | 3,27 | 1,00x |
| 2 | 4,88 | 1,49x |
| 4 | 8,62 | 2,64x |
| 6 | 10,89 | 3,33x |
| 8 | 12,08 | 3,70x |
| 12 | 13,05 | 4,00x |

Conviene saberlo antes de recurrir a `split`: solo con los hilos ya tienes 4x en
una única máquina, así que repartir entre varias es para cuando eso no baste.

`split` se verificó contra una captura real con 3 y 5 piezas — las muestras de
cada pieza coinciden exactamente con el original en el offset que declara.
`merge` e `insert` se verificaron sobre ficheros `.tbc` construidos donde cada
campo lleva un patrón que identifica su posición.
