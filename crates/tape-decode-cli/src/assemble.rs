//! Putting `.tbc` decodes of one tape back together.
//!
//! `merge` joins decodes that follow on from each other - the results of
//! decoding the pieces [`crate::split`] produced, possibly on different
//! machines. `insert` fills a gap in the middle of a finished decode, for when
//! one machine's piece failed and had to be decoded again afterwards.
//!
//! Neighbouring decodes overlap on purpose, each one re-locks sync at its own
//! starting point, and field parity has to keep alternating across every join or
//! every frame after it is assembled from the wrong pair of fields. Those three
//! things are what this module is for.

use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use anyhow::{bail, Context as _, Result};
use tape_decode::FieldInfoEntry;

use crate::metadata::TbcMetadataFull;

/// One decode to be placed on the tape.
pub(crate) struct Part {
    pub(crate) tbc: PathBuf,
    pub(crate) chroma: Option<PathBuf>,
    pub(crate) meta: TbcMetadataFull,
    /// Absolute sample offset of this decode's piece within the whole capture,
    /// added to every `file_loc`. A decode numbers its fields from the start of
    /// its own input, so without this a piece cut from the middle of a capture
    /// would be placed at the beginning.
    pub(crate) origin: u64,
    pub(crate) label: String,
}

impl Part {
    pub(crate) fn load(tbc: &Path, origin: u64, label: String) -> Result<Self> {
        let json = sidecar_path(tbc);
        let text = std::fs::read_to_string(&json)
            .with_context(|| format!("no metadata beside {}", tbc.display()))?;
        let meta: TbcMetadataFull = serde_json::from_str(&text)
            .with_context(|| format!("could not parse {}", json.display()))?;
        let chroma = chroma_path(tbc);
        Ok(Self {
            tbc: tbc.to_path_buf(),
            chroma: chroma.exists().then_some(chroma),
            meta,
            origin,
            label,
        })
    }

    /// This decode's field positions, on the whole capture's timeline.
    pub(crate) fn locs(&self) -> Vec<u64> {
        self.meta
            .fields
            .iter()
            .map(|f| f.file_loc + self.origin)
            .collect()
    }

    fn field_bytes(&self) -> usize {
        self.meta.video_parameters.field_width * self.meta.video_parameters.field_height * 2
    }
}

pub(crate) fn sidecar_path(tbc: &Path) -> PathBuf {
    let mut s = tbc.as_os_str().to_os_string();
    s.push(".json");
    PathBuf::from(s)
}

pub(crate) fn chroma_path(tbc: &Path) -> PathBuf {
    let stem = tbc
        .to_string_lossy()
        .strip_suffix(".tbc")
        .map(str::to_string)
        .unwrap_or_else(|| tbc.to_string_lossy().into_owned());
    PathBuf::from(format!("{stem}_chroma.tbc"))
}

/// Where each part stops, as an absolute sample offset; `None` for the last.
///
/// Cut each part where the next one that actually produced fields started,
/// rather than at a nominal split point: a decoder starting at sample N may lock
/// on a field that begins slightly before N, and cutting at N would drop it from
/// both sides, leaving a hole. Skipping over empty parts matters too - cutting
/// at an empty part's bound would let the previous part's overrun duplicate the
/// content of the part after it.
fn cut_points(parts: &[Part]) -> Vec<Option<u64>> {
    (0..parts.len())
        .map(|i| {
            parts[i + 1..]
                .iter()
                .find_map(|p| p.locs().first().copied())
        })
        .collect()
}

/// The fields of a part that belong to it rather than to the next one.
fn kept_indices(locs: &[u64], limit: Option<u64>) -> Vec<usize> {
    match limit {
        None => (0..locs.len()).collect(),
        Some(limit) => locs.iter().take_while(|&&l| l < limit).enumerate().map(|(i, _)| i).collect(),
    }
}

/// Open a `.tbc` and report how many whole fields it actually holds.
///
/// A decode that was killed part-way can leave a sidecar listing fields that
/// never reached the disk; copying those would emit truncated fields.
fn open_checked(
    path: &Path,
    field_bytes: usize,
    listed: usize,
    label: &str,
    warn: &mut impl FnMut(String),
) -> Result<(File, usize)> {
    let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
    let have = (file.metadata()?.len() / field_bytes as u64) as usize;
    if have < listed {
        warn(format!(
            "{label}: {} holds {have} field(s) but its metadata lists {listed} - using what is on disk",
            path.file_name().unwrap_or_default().to_string_lossy()
        ));
    }
    Ok((file, have))
}

pub(crate) struct MergeOutcome {
    pub(crate) fields: usize,
    pub(crate) dropped_parity: usize,
}

/// Write `out_base.tbc` / `_chroma.tbc` / `.tbc.json` from `parts`, in order.
///
/// `on_part_done` is called once each input has been fully copied, so a caller
/// can free its disk space before the next one is read - merging a whole tape
/// otherwise needs room for a second copy of it.
pub(crate) fn merge(
    parts: &[Part],
    out_base: &Path,
    mut warn: impl FnMut(String),
    mut on_part_done: impl FnMut(usize),
) -> Result<MergeOutcome> {
    let first = parts.first().context("nothing to merge")?;
    let field_bytes = first.field_bytes();
    if field_bytes == 0 {
        bail!("the first decode reports a zero-sized field");
    }
    let cut_at = cut_points(parts);
    let want_chroma = first.chroma.is_some();

    let mut out_tbc = File::create(with_ext(out_base, ".tbc"))?;
    let mut out_chroma = want_chroma
        .then(|| File::create(with_ext(out_base, "_chroma.tbc")))
        .transpose()?;

    let mut all_fields: Vec<FieldInfoEntry> = Vec::new();
    let mut dropped_parity = 0usize;
    let mut prev_is_first: Option<bool> = None;
    let mut buf = vec![0u8; field_bytes];

    for (i, part) in parts.iter().enumerate() {
        if part.field_bytes() != field_bytes {
            bail!(
                "{} has a different field size to the first decode; these are not \
                 from the same capture and system",
                part.label
            );
        }
        let locs = part.locs();
        let mut keep = kept_indices(&locs, cut_at[i]);
        if keep.is_empty() {
            warn(format!("{} contributed no fields", part.label));
            on_part_done(i);
            continue;
        }

        // Keep first/second field alternating across the join.
        if let Some(prev) = prev_is_first {
            if part.meta.fields[keep[0]].is_first_field == prev {
                keep.remove(0);
                dropped_parity += 1;
                if keep.is_empty() {
                    on_part_done(i);
                    continue;
                }
            }
        }

        let (mut src, available) =
            open_checked(&part.tbc, field_bytes, part.meta.fields.len(), &part.label, &mut warn)?;
        let mut chroma_src = match (&part.chroma, out_chroma.is_some()) {
            (Some(path), true) => {
                let (f, avail) = open_checked(
                    path,
                    field_bytes,
                    part.meta.fields.len(),
                    &part.label,
                    &mut warn,
                )?;
                Some((f, avail))
            }
            (None, true) => bail!(
                "{} has no chroma file but an earlier decode did; the output would \
                 be misaligned",
                part.label
            ),
            _ => None,
        };
        // Luma and chroma must stay field-aligned, so a short chroma file limits
        // both.
        let available = chroma_src
            .as_ref()
            .map_or(available, |(_, avail)| available.min(*avail));

        // Copy field by field rather than slurping the file: one machine's share
        // of a tape is gigabytes.
        for &idx in &keep {
            if idx >= available {
                break;
            }
            src.seek(SeekFrom::Start((idx * field_bytes) as u64))?;
            if src.read_exact(&mut buf).is_err() {
                break;
            }
            out_tbc.write_all(&buf)?;
            if let (Some(out), Some((cf, _))) = (out_chroma.as_mut(), chroma_src.as_mut()) {
                cf.seek(SeekFrom::Start((idx * field_bytes) as u64))?;
                cf.read_exact(&mut buf)?;
                out.write_all(&buf)?;
            }
            let mut entry = part.meta.fields[idx].clone();
            entry.file_loc = locs[idx];
            entry.seq_no = all_fields.len() + 1;
            prev_is_first = Some(entry.is_first_field);
            all_fields.push(entry);
        }

        drop(src);
        drop(chroma_src.take());
        on_part_done(i);
    }

    out_tbc.flush()?;
    if let Some(out) = out_chroma.as_mut() {
        out.flush()?;
    }
    write_sidecar(out_base, first, all_fields.len(), &all_fields)?;

    Ok(MergeOutcome {
        fields: all_fields.len(),
        dropped_parity,
    })
}

pub(crate) struct InsertPlan {
    /// Index in the target where the insert goes.
    pub(crate) at: usize,
    /// Indices of the insert's fields that fall inside the gap.
    pub(crate) keep: Vec<usize>,
    pub(crate) gap_start: u64,
    pub(crate) gap_end: u64,
}

/// Work out which fields of `insert` fill the largest gap in `target`.
pub(crate) fn plan_insert(
    target: &Part,
    insert: &Part,
    samples_per_field: f64,
    mut warn: impl FnMut(String),
) -> Result<InsertPlan> {
    let locs = target.locs();
    if locs.len() < 2 {
        bail!("the target has too few fields to contain a gap");
    }
    let (at, gap) = locs
        .windows(2)
        .enumerate()
        .map(|(i, w)| (i + 1, w[1] - w[0]))
        .max_by_key(|&(_, g)| g)
        .expect("at least one pair");
    if (gap as f64) < 4.0 * samples_per_field {
        bail!("no gap found in the target - there is nothing to insert into");
    }
    let (gap_start, gap_end) = (locs[at - 1], locs[at]);

    let ins = insert.locs();
    if !(ins[0] < gap_end && *ins.last().unwrap() > gap_start) {
        bail!(
            "the insert covers a different stretch of tape to the gap; these do not overlap"
        );
    }

    // Keep only what falls in the hole; the decode was asked to start early and
    // run long so it could lock sync, and that overrun is already present on
    // both sides.
    let mut keep: Vec<usize> = ins
        .iter()
        .enumerate()
        .filter(|(_, &l)| l > gap_start && l < gap_end)
        .map(|(i, _)| i)
        .collect();
    if keep.is_empty() {
        bail!("the insert has no fields inside the gap");
    }

    // Parity has to alternate across both new joins.
    let before = target.meta.fields[at - 1].is_first_field;
    if insert.meta.fields[keep[0]].is_first_field == before {
        keep.remove(0);
        warn("dropping one field at the start of the insert to keep parity".into());
    }
    let after = target.meta.fields[at].is_first_field;
    while let Some(&last) = keep.last() {
        if insert.meta.fields[last].is_first_field == after {
            keep.pop();
            warn("dropping one field at the end of the insert to keep parity".into());
        } else {
            break;
        }
    }
    if keep.is_empty() {
        bail!("nothing left of the insert after fixing parity");
    }

    Ok(InsertPlan {
        at,
        keep,
        gap_start,
        gap_end,
    })
}

/// Open a hole at field `at` and write the insert's fields into it, in place.
///
/// Splitting the file and re-merging would need room for a second copy of the
/// whole decode. This extends the file and shifts the tail along instead, moving
/// it backwards from its end so no field is overwritten before it has been
/// copied; the only extra space needed is the insert itself.
fn shift_and_write(
    target: &Path,
    source: &Path,
    field_bytes: usize,
    at: usize,
    keep: &[usize],
    existing: usize,
    mut progress: impl FnMut(u64, u64),
) -> Result<()> {
    let count = keep.len();
    let tail = existing - at;
    let mut dst = OpenOptions::new().read(true).write(true).open(target)?;
    let mut src = File::open(source)?;

    dst.set_len(((existing + count) * field_bytes) as u64)?;

    let chunk_fields = (8 * 1024 * 1024 / field_bytes).max(1);
    let mut buf = vec![0u8; chunk_fields * field_bytes];
    let mut moved = 0usize;
    while moved < tail {
        let n = chunk_fields.min(tail - moved);
        let src_first = at + tail - moved - n;
        dst.seek(SeekFrom::Start((src_first * field_bytes) as u64))?;
        dst.read_exact(&mut buf[..n * field_bytes])
            .context("short read while shifting the tail")?;
        dst.seek(SeekFrom::Start(((src_first + count) * field_bytes) as u64))?;
        dst.write_all(&buf[..n * field_bytes])?;
        moved += n;
        progress(moved as u64, tail as u64);
    }

    dst.seek(SeekFrom::Start((at * field_bytes) as u64))?;
    let mut one = vec![0u8; field_bytes];
    for &idx in keep {
        src.seek(SeekFrom::Start((idx * field_bytes) as u64))?;
        src.read_exact(&mut one)
            .context("short read from the insert")?;
        dst.write_all(&one)?;
    }
    dst.flush()?;
    Ok(())
}

/// Carry out `plan`, rewriting `target` in place.
///
/// The sidecar is written last: until then the file still matches its old index,
/// which is what makes an interrupted run recoverable.
pub(crate) fn insert(
    target: &Part,
    source: &Part,
    plan: &InsertPlan,
    mut progress: impl FnMut(&str, u64, u64),
) -> Result<usize> {
    let field_bytes = target.field_bytes();
    if source.field_bytes() != field_bytes {
        bail!("the two decodes have different field sizes; they are not from the same capture");
    }
    let existing = target.meta.fields.len();
    let on_disk = (std::fs::metadata(&target.tbc)?.len() / field_bytes as u64) as usize;
    if on_disk < existing {
        bail!(
            "{} holds {on_disk} fields but its metadata lists {existing}; refusing to \
             shift a file that does not match its own index",
            target.tbc.display()
        );
    }

    shift_and_write(
        &target.tbc,
        &source.tbc,
        field_bytes,
        plan.at,
        &plan.keep,
        existing,
        |a, b| progress("luma", a, b),
    )?;
    if let (Some(dst), Some(src)) = (&target.chroma, &source.chroma) {
        shift_and_write(dst, src, field_bytes, plan.at, &plan.keep, existing, |a, b| {
            progress("chroma", a, b)
        })?;
    }

    let mut fields = target.meta.fields[..plan.at].to_vec();
    fields.extend(plan.keep.iter().map(|&i| {
        let mut e = source.meta.fields[i].clone();
        e.file_loc = source.meta.fields[i].file_loc + source.origin;
        e
    }));
    fields.extend_from_slice(&target.meta.fields[plan.at..]);
    for (n, f) in fields.iter_mut().enumerate() {
        f.seq_no = n + 1;
    }
    let total = fields.len();
    let base = strip_ext(&target.tbc);
    write_sidecar(&base, target, total, &fields)?;
    Ok(total)
}

fn with_ext(base: &Path, suffix: &str) -> PathBuf {
    let mut s = base.as_os_str().to_os_string();
    s.push(suffix);
    PathBuf::from(s)
}

fn strip_ext(tbc: &Path) -> PathBuf {
    let s = tbc.to_string_lossy();
    PathBuf::from(s.strip_suffix(".tbc").unwrap_or(&s).to_string())
}

fn write_sidecar(
    out_base: &Path,
    template: &Part,
    count: usize,
    fields: &[FieldInfoEntry],
) -> Result<()> {
    let mut meta = serde_json::json!({
        "pcmAudioParameters": template.meta.pcm_audio_parameters,
        "videoParameters": template.meta.video_parameters,
        "fields": fields,
    });
    meta["videoParameters"]["numberOfSequentialFields"] = serde_json::json!(count);
    let path = with_ext(out_base, ".tbc.json");
    let file = File::create(&path).with_context(|| format!("writing {}", path.display()))?;
    serde_json::to_writer(file, &meta)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kept_indices_stops_at_the_cut() {
        let locs = [0u64, 100, 200, 300, 400];
        assert_eq!(kept_indices(&locs, Some(300)), vec![0, 1, 2]);
        assert_eq!(kept_indices(&locs, None), vec![0, 1, 2, 3, 4]);
        assert_eq!(kept_indices(&locs, Some(0)), Vec::<usize>::new());
    }

    #[test]
    fn chroma_and_sidecar_paths() {
        let tbc = Path::new("out.tbc");
        assert_eq!(chroma_path(tbc), Path::new("out_chroma.tbc"));
        assert_eq!(sidecar_path(tbc), Path::new("out.tbc.json"));
    }
}
