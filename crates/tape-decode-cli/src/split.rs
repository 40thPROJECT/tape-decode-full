//! Cutting an RF capture into standalone pieces, to decode on several machines.
//!
//! `--mt-threads` spreads one decode across the cores of one machine. This
//! spreads a capture across machines: each piece is a complete capture file that
//! any build of this decoder can open on its own, and [`crate::assemble`] puts
//! the resulting `.tbc` files back on one timeline.
//!
//! FLAC frames are self-contained, so a file made of the original metadata
//! headers followed by a run of whole frames is a valid FLAC that decodes to
//! that stretch of tape. Splitting is therefore a byte copy - nothing is
//! re-encoded and the pieces are bit-identical to the corresponding samples of
//! the original. Raw and packed captures split the same way, at sample
//! boundaries.
//!
//! Boundaries are found by bisecting the file and resynchronising on frame
//! headers rather than by asking the container where a timestamp lives. A
//! capture past 2^36 samples cannot record its own length in a FLAC header, and
//! decoders that estimate positions from the bitrate can be wildly wrong on one
//! - 25x short, on a 2h45m capture that prompted this. Frame headers carry their
//! own position and are always right.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use anyhow::{bail, Context as _, Result};
use serde::{Deserialize, Serialize};

use crate::reader::SampleFormat;

/// Sync word of a fixed-blocksize FLAC frame: 14 sync bits, a zero reserved
/// bit, and a zero blocking-strategy bit.
const FRAME_SYNC: [u8; 2] = [0xFF, 0xF8];

/// Longest possible frame header: sync, two parameter bytes, a 7-byte coded
/// number, optional blocksize and sample rate fields, and the CRC.
const MAX_HEADER: usize = 16;

/// Once the bracket is this small, walking the remaining frames beats another
/// bisection step.
const SETTLE: u64 = 1 << 16;

/// Where one piece of a capture sits on the tape, as written to the manifest.
#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct PieceInfo {
    /// File name of the piece, relative to the manifest.
    pub(crate) file: String,
    /// Absolute sample offset of this piece's first sample within the capture.
    /// This is what places a decode of the piece back on the tape.
    pub(crate) start_sample: u64,
    /// Samples the piece holds, as far as the split could determine.
    pub(crate) samples: u64,
    pub(crate) bytes: u64,
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Manifest {
    pub(crate) source: String,
    pub(crate) source_bytes: u64,
    pub(crate) source_samples: u64,
    /// Samples each piece repeats from the one before, so a decoder has lead-in
    /// to lock sync; [`crate::assemble`] trims it back out.
    pub(crate) overlap_samples: u64,
    pub(crate) parts: Vec<PieceInfo>,
}

/// What the split will write, resolved before a byte is copied so the caller can
/// report progress against a known total.
struct Piece {
    file: String,
    start_sample: u64,
    samples: u64,
    byte_start: u64,
    byte_end: u64,
    header: Vec<u8>,
}

// -- FLAC ------------------------------------------------------------------

/// CRC-8 with polynomial x^8 + x^2 + x + 1, as FLAC uses for frame headers.
fn crc8(data: &[u8]) -> u8 {
    let mut crc: u8 = 0;
    for &byte in data {
        crc ^= byte;
        for _ in 0..8 {
            crc = if crc & 0x80 != 0 {
                (crc << 1) ^ 0x07
            } else {
                crc << 1
            };
        }
    }
    crc
}

/// Decode FLAC's UTF-8-style coded number. Returns the value and its length.
fn read_coded_number(data: &[u8]) -> Option<(u64, usize)> {
    let first = *data.first()?;
    if first < 0x80 {
        return Some((u64::from(first), 1));
    }
    let len = (first.leading_ones()) as usize;
    if !(2..=7).contains(&len) || data.len() < len {
        return None;
    }
    let mut value = u64::from(first & (0x7F >> len));
    for &byte in &data[1..len] {
        if byte & 0xC0 != 0x80 {
            return None;
        }
        value = (value << 6) | u64::from(byte & 0x3F);
    }
    Some((value, len))
}

/// The parts of STREAMINFO this needs, plus where the frames begin.
struct FlacInfo {
    /// Metadata headers verbatim, reused at the front of every piece.
    header: Vec<u8>,
    first_frame_offset: u64,
    blocksize: u64,
    max_framesize: u64,
    size: u64,
}

impl FlacInfo {
    fn read(path: &Path) -> Result<Self> {
        let size = std::fs::metadata(path)?.len();
        let mut file = File::open(path)?;
        let mut magic = [0u8; 4];
        file.read_exact(&mut magic)?;
        if &magic != b"fLaC" {
            bail!("not a raw FLAC stream (no fLaC magic)");
        }

        let mut pos: u64 = 4;
        let mut blocksize = 0u64;
        let mut max_framesize = 0u64;
        loop {
            let mut head = [0u8; 4];
            file.read_exact(&mut head).context("truncated metadata")?;
            let is_last = head[0] & 0x80 != 0;
            let block_type = head[0] & 0x7F;
            let length = u32::from_be_bytes([0, head[1], head[2], head[3]]) as usize;
            let mut body = vec![0u8; length];
            file.read_exact(&mut body).context("truncated metadata")?;
            if block_type == 0 {
                if length < 34 {
                    bail!("short STREAMINFO");
                }
                let min_bs = u64::from(u16::from_be_bytes([body[0], body[1]]));
                let max_bs = u64::from(u16::from_be_bytes([body[2], body[3]]));
                if min_bs != max_bs {
                    // Variable-blocksize streams code a sample number rather
                    // than a frame number.  That is still usable, but nothing
                    // that writes these captures produces them, and an untested
                    // path is worse than declining.
                    bail!("variable blocksize FLAC is not supported");
                }
                blocksize = min_bs;
                max_framesize = u64::from(u32::from_be_bytes([0, body[7], body[8], body[9]]));
            }
            pos += 4 + length as u64;
            if is_last {
                break;
            }
        }
        if blocksize == 0 {
            bail!("STREAMINFO has no blocksize");
        }

        file.seek(SeekFrom::Start(0))?;
        let mut header = vec![0u8; pos as usize];
        file.read_exact(&mut header)?;

        Ok(Self {
            header,
            first_frame_offset: pos,
            blocksize,
            // Only seeds the first interpolation step; STREAMINFO may leave it
            // at zero, and an uncompressed frame is the safe upper bound.
            max_framesize: if max_framesize > 0 {
                max_framesize
            } else {
                blocksize * 4 + 16
            },
            size,
        })
    }

    /// First sample of the frame starting at `buf[at..]`, if that really is a
    /// frame header.
    fn parse_frame_header(&self, buf: &[u8], at: usize) -> Option<u64> {
        let win = buf.get(at..at + MAX_HEADER)?;
        if win[0] != FRAME_SYNC[0] || win[1] != FRAME_SYNC[1] {
            return None;
        }
        let blocksize_bits = win[2] >> 4;
        let samplerate_bits = win[2] & 0x0F;
        let channel_bits = win[3] >> 4;
        if win[3] & 0x01 != 0 || blocksize_bits == 0 || samplerate_bits == 0x0F {
            return None;
        }
        // Cross-check against STREAMINFO: a random byte pair that happens to
        // look like a sync word almost never agrees on all of these.
        let coded_blocksize = match blocksize_bits {
            1 => Some(192),
            n @ 2..=5 => Some(576u64 << (n - 2)),
            n @ 8..=15 => Some(256u64 << (n - 8)),
            6 | 7 => None, // read from the end of the header; accept it
            _ => return None,
        };
        if let Some(bs) = coded_blocksize {
            if bs != self.blocksize {
                return None;
            }
        }
        if channel_bits < 8 && u64::from(channel_bits) + 1 != 1 {
            return None;
        }

        let (number, coded_len) = read_coded_number(&win[4..])?;
        let mut end = 4 + coded_len;
        end += match blocksize_bits {
            6 => 1,
            7 => 2,
            _ => 0,
        };
        end += match samplerate_bits {
            12 => 1,
            13 | 14 => 2,
            _ => 0,
        };
        if end >= win.len() {
            return None;
        }
        if crc8(&win[..end]) != win[end] {
            return None;
        }
        Some(number * self.blocksize)
    }

    /// Is there a frame right after `offset` whose number follows on? A lone
    /// valid-looking header can occur by chance inside compressed audio.
    fn confirms(&self, file: &mut File, offset: u64, sample: u64) -> Result<bool> {
        let span = (self.max_framesize * 2 + 64) as usize;
        let mut buf = vec![0u8; span];
        file.seek(SeekFrom::Start(offset))?;
        let read = read_upto(file, &mut buf)?;
        let buf = &buf[..read];
        let mut i = 1usize;
        while i + MAX_HEADER <= buf.len() {
            match find_sync(buf, i) {
                Some(found) => {
                    if let Some(next) = self.parse_frame_header(buf, found) {
                        return Ok(next == sample + self.blocksize);
                    }
                    i = found + 1;
                }
                None => break,
            }
        }
        // Ran out of file: the last frame has nothing to confirm it.
        Ok(offset + span as u64 >= self.size)
    }

    /// First valid frame at or after `from`, bounded by `stop`.
    fn scan_forward(&self, file: &mut File, from: u64, stop: u64) -> Result<Option<(u64, u64)>> {
        const WINDOW: usize = 1 << 16;
        let mut pos = from.max(self.first_frame_offset);
        let end = stop.min(self.size);
        let mut searched: u64 = 0;
        let mut buf = vec![0u8; WINDOW];
        while pos < end && searched < (1 << 22) {
            file.seek(SeekFrom::Start(pos))?;
            let read = read_upto(file, &mut buf)?;
            if read < MAX_HEADER {
                return Ok(None);
            }
            let window = &buf[..read];
            let limit = read - MAX_HEADER;
            let mut i = 0usize;
            while let Some(found) = find_sync(window, i) {
                if found >= limit {
                    break;
                }
                if let Some(sample) = self.parse_frame_header(window, found) {
                    if self.confirms(file, pos + found as u64, sample)? {
                        return Ok(Some((pos + found as u64, sample)));
                    }
                }
                i = found + 1;
            }
            pos += (WINDOW - MAX_HEADER) as u64;
            searched += (WINDOW - MAX_HEADER) as u64;
        }
        Ok(None)
    }

    /// Byte offset and first sample of the frame holding `target`, counted from
    /// the file's first frame.
    ///
    /// False position with a forced plain bisection every other step.
    /// Interpolation alone stalls: when the estimated rate is very close to the
    /// truth the guess lands one byte below the bound, finds the same frame
    /// again, and the bracket shrinks by a byte per iteration.
    fn find_frame(&self, file: &mut File, target: u64) -> Result<(u64, u64)> {
        let first = self
            .scan_forward(file, self.first_frame_offset, self.size)?
            .context("no frame header found at the start of the file")?;
        let base = first.1;
        let target = base + target;
        if target <= base {
            return Ok((first.0, 0));
        }

        let (mut lo_off, mut lo_sample) = first;
        let mut hi_off = self.size;
        let mut hi_sample: Option<u64> = None;
        let mut bisect_turn = false;

        while hi_off - lo_off > SETTLE {
            let span = hi_off - lo_off;
            let guess = if bisect_turn {
                lo_off + span / 2
            } else {
                let frac = match hi_sample {
                    Some(hs) if hs > lo_sample => {
                        (target - lo_sample) as f64 / (hs - lo_sample) as f64
                    }
                    _ => {
                        let seen_bytes = lo_off - first.0;
                        let seen_samples = lo_sample - base;
                        let rate = if seen_bytes > 0 && seen_samples > 0 {
                            seen_samples as f64 / seen_bytes as f64
                        } else {
                            // Nothing measured yet: a fully packed frame is the
                            // densest the file can be, so this errs towards
                            // guessing too far in, establishing an upper bound.
                            self.blocksize as f64 / self.max_framesize as f64
                        };
                        let span_samples = span as f64 * rate;
                        if span_samples > 0.0 {
                            (target - lo_sample) as f64 / span_samples
                        } else {
                            0.5
                        }
                    }
                };
                lo_off + (span as f64 * frac.clamp(0.0, 1.0)) as u64
            };
            bisect_turn = !bisect_turn;
            // Keep the step strictly inside the bracket so it always shrinks.
            let guess = guess.clamp(lo_off + 1, hi_off - 1);

            match self.scan_forward(file, guess, hi_off)? {
                None => hi_off = guess,
                Some((off, sample)) => {
                    if sample <= target {
                        lo_off = off;
                        lo_sample = sample;
                    } else {
                        // `off` is the first frame at or after the guess and is
                        // already past the target, so nothing from the guess on
                        // can qualify.  Pulling back to the guess rather than to
                        // `off` also guarantees the bracket shrinks - `off` can
                        // equal the current bound, which would loop forever.
                        hi_off = guess;
                        hi_sample = Some(sample);
                    }
                }
            }
        }

        // Walk the last stretch frame by frame for an exact answer.
        let mut best = (lo_off, lo_sample);
        let mut probe = lo_off;
        while let Some(found) = self.scan_forward(file, probe + 1, hi_off + SETTLE)? {
            if found.1 > target {
                break;
            }
            best = found;
            probe = found.0;
        }
        Ok((best.0, best.1 - base))
    }

    /// The file's own frame numbering start, so a piece cut from a longer
    /// capture can report where it really sits on the tape.
    fn base_sample(&self, file: &mut File) -> Result<u64> {
        Ok(self
            .scan_forward(file, self.first_frame_offset, self.size)?
            .context("no frame header found at the start of the file")?
            .1)
    }
}

fn find_sync(buf: &[u8], from: usize) -> Option<usize> {
    buf.get(from..)?
        .windows(2)
        .position(|w| w == FRAME_SYNC)
        .map(|i| i + from)
}

fn read_upto(file: &mut File, buf: &mut [u8]) -> Result<usize> {
    let mut filled = 0;
    while filled < buf.len() {
        match file.read(&mut buf[filled..])? {
            0 => break,
            n => filled += n,
        }
    }
    Ok(filled)
}

/// Give a piece's headers its own sample count, and drop the stream MD5.
///
/// A piece inherits the source's STREAMINFO, which describes the whole capture,
/// so every piece would claim the length of the original. The MD5 covers the
/// original samples and can never match a piece; all-zero is FLAC's "not
/// computed".
fn restamp(header: &[u8], total_samples: u64) -> Vec<u8> {
    let body = 8usize; // after "fLaC" and the 4-byte block header
    let mut out = header.to_vec();
    if out.len() < body + 34 || &out[..4] != b"fLaC" {
        return out;
    }
    let mut packed = u64::from_be_bytes(out[body + 10..body + 18].try_into().unwrap());
    const MASK: u64 = (1 << 36) - 1;
    packed = (packed & !MASK) | total_samples.min(MASK);
    out[body + 10..body + 18].copy_from_slice(&packed.to_be_bytes());
    out[body + 18..body + 34].fill(0);
    out
}

// -- planning --------------------------------------------------------------

/// Bytes per group and samples per group, for formats cut at sample boundaries.
fn raw_layout(format: SampleFormat) -> (u64, u64) {
    match format {
        SampleFormat::U8 | SampleFormat::S8 => (1, 1),
        SampleFormat::S16LE | SampleFormat::U16LE => (2, 1),
        SampleFormat::F32LE => (4, 1),
        SampleFormat::Flac => unreachable!("FLAC is planned separately"),
    }
}

/// `(start, end)` sample spans, each overlapping the one before by `overlap`.
///
/// An overlap approaching the size of a piece would have every piece starting
/// near the front of the capture, copying the same stretch of tape over and
/// over, so it is capped at a quarter of a piece.
fn plan_cuts(total: u64, parts: u64, overlap: u64) -> (Vec<(u64, Option<u64>)>, u64) {
    let piece = total as f64 / parts as f64;
    let capped = overlap.min((piece / 4.0) as u64);
    let cuts = (0..parts)
        .map(|i| {
            let nominal = (i as f64 * piece) as u64;
            let start = if i == 0 {
                0
            } else {
                nominal.saturating_sub(capped)
            };
            let end = if i == parts - 1 {
                None
            } else {
                Some(((i + 1) as f64 * piece) as u64)
            };
            (start, end)
        })
        .collect();
    (cuts, capped)
}

fn plan_flac(path: &Path, stem: &str, cuts: &[(u64, Option<u64>)]) -> Result<Vec<Piece>> {
    let info = FlacInfo::read(path)?;
    let mut file = File::open(path)?;
    let base = info.base_sample(&mut file)?;
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("ldf")
        .to_string();

    let mut pieces = Vec::with_capacity(cuts.len());
    for (i, (start, end)) in cuts.iter().enumerate() {
        let (byte_start, first_sample) = info.find_frame(&mut file, *start)?;
        let (byte_end, span) = match end {
            Some(e) => {
                let (b, end_sample) = info.find_frame(&mut file, *e)?;
                (b, end_sample.saturating_sub(first_sample))
            }
            None => {
                // Runs to EOF; estimate the span from the bytes, only to stamp
                // the header.
                let seen = byte_start.saturating_sub(info.first_frame_offset);
                let span = if seen > 0 {
                    ((info.size - byte_start) as f64 * (first_sample as f64 / seen as f64)) as u64
                } else {
                    0
                };
                (info.size, span)
            }
        };
        pieces.push(Piece {
            file: format!("{stem}.part{i:02}.{ext}"),
            start_sample: base + first_sample,
            samples: span,
            byte_start,
            byte_end,
            header: restamp(&info.header, span),
        });
    }
    Ok(pieces)
}

fn plan_raw(
    path: &Path,
    stem: &str,
    format: SampleFormat,
    cuts: &[(u64, Option<u64>)],
) -> Result<Vec<Piece>> {
    let (group_bytes, group_samples) = raw_layout(format);
    let total = std::fs::metadata(path)?.len();
    let to_byte = |sample: u64| -> u64 { (sample / group_samples * group_bytes).min(total) };
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("raw")
        .to_string();

    Ok(cuts
        .iter()
        .enumerate()
        .map(|(i, (start, end))| {
            let byte_start = to_byte(*start);
            let byte_end = end.map_or(total, to_byte);
            Piece {
                file: format!("{stem}.part{i:02}.{ext}"),
                start_sample: byte_start / group_bytes * group_samples,
                samples: (byte_end - byte_start) / group_bytes * group_samples,
                byte_start,
                byte_end,
                header: Vec::new(),
            }
        })
        .collect())
}

// -- running ---------------------------------------------------------------

pub(crate) struct SplitRequest<'a> {
    pub(crate) input: &'a Path,
    pub(crate) out_dir: &'a Path,
    pub(crate) format: SampleFormat,
    pub(crate) parts: u64,
    pub(crate) overlap_samples: u64,
    pub(crate) total_samples: u64,
}

pub(crate) fn run(req: SplitRequest<'_>, mut progress: impl FnMut(u64, u64)) -> Result<Manifest> {
    if req.parts == 0 {
        bail!("--parts must be at least 1");
    }
    let stem = req
        .input
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("capture")
        .to_string();

    let (cuts, overlap) = plan_cuts(req.total_samples, req.parts, req.overlap_samples);
    let pieces = match req.format {
        SampleFormat::Flac => plan_flac(req.input, &stem, &cuts)?,
        other => plan_raw(req.input, &stem, other, &cuts)?,
    };

    // Progress is measured against what will actually be written, not the size
    // of the source: asking for a slice of a large capture copies a fraction of
    // it, and a bar counting up to the whole file would be meaningless.
    let to_write: u64 = pieces
        .iter()
        .map(|p| p.byte_end - p.byte_start + p.header.len() as u64)
        .sum();

    std::fs::create_dir_all(req.out_dir)?;
    let mut src = File::open(req.input)?;
    let mut written = 0u64;
    let mut parts = Vec::with_capacity(pieces.len());
    let mut buf = vec![0u8; 8 * 1024 * 1024];

    for piece in &pieces {
        let dst_path: PathBuf = req.out_dir.join(&piece.file);
        let mut dst = File::create(&dst_path)
            .with_context(|| format!("creating {}", dst_path.display()))?;
        dst.write_all(&piece.header)?;
        src.seek(SeekFrom::Start(piece.byte_start))?;
        let mut remaining = piece.byte_end - piece.byte_start;
        while remaining > 0 {
            let want = remaining.min(buf.len() as u64) as usize;
            let read = src.read(&mut buf[..want])?;
            if read == 0 {
                break;
            }
            dst.write_all(&buf[..read])?;
            remaining -= read as u64;
            written += read as u64;
            progress(written, to_write);
        }
        dst.flush()?;
        parts.push(PieceInfo {
            file: piece.file.clone(),
            start_sample: piece.start_sample,
            samples: piece.samples,
            bytes: piece.byte_end - piece.byte_start + piece.header.len() as u64,
        });
    }

    Ok(Manifest {
        source: req
            .input
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_string(),
        source_bytes: std::fs::metadata(req.input)?.len(),
        source_samples: req.total_samples,
        overlap_samples: overlap,
        parts,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc8_matches_known_frame_header() {
        // First frame header of a real 40 MSPS capture: fixed blocksize 2048,
        // mono, 16-bit, frame 0.  The CRC byte is 0xC8.
        let header = [0xFF, 0xF8, 0xB0, 0x08, 0x00];
        assert_eq!(crc8(&header), 0xC8);
    }

    #[test]
    fn coded_number_round_trips() {
        assert_eq!(read_coded_number(&[0x00]), Some((0, 1)));
        assert_eq!(read_coded_number(&[0x7F]), Some((127, 1)));
        // 0xC2 0x80 encodes 128 in FLAC's UTF-8-style scheme.
        assert_eq!(read_coded_number(&[0xC2, 0x80]), Some((128, 2)));
        // A continuation byte that is not 10xxxxxx is not a valid number.
        assert_eq!(read_coded_number(&[0xC2, 0x00]), None);
    }

    #[test]
    fn overlap_is_capped_to_a_quarter_of_a_piece() {
        let (cuts, overlap) = plan_cuts(1000, 4, 10_000);
        assert_eq!(overlap, 62, "an absurd overlap must be capped");
        assert_eq!(cuts[0].0, 0, "the first piece always starts at zero");
        assert!(cuts[1].0 < 250, "later pieces start before their bound");
        assert!(cuts.last().unwrap().1.is_none(), "the last piece runs to EOF");
    }

    #[test]
    fn cuts_cover_the_capture_without_gaps() {
        let (cuts, _) = plan_cuts(9_000, 3, 0);
        assert_eq!(cuts[0], (0, Some(3_000)));
        assert_eq!(cuts[1], (3_000, Some(6_000)));
        assert_eq!(cuts[2], (6_000, None));
    }

    #[test]
    fn restamp_rewrites_the_sample_count_and_clears_the_md5() {
        let mut header = vec![0u8; 42];
        header[..4].copy_from_slice(b"fLaC");
        header[8 + 18..8 + 34].fill(0xAB);
        let out = restamp(&header, 12345);
        let packed = u64::from_be_bytes(out[18..26].try_into().unwrap());
        assert_eq!(packed & ((1 << 36) - 1), 12345);
        assert!(out[26..42].iter().all(|&b| b == 0), "MD5 must be cleared");
    }
}
