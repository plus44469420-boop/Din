//! Module SDK stamps: the subset of ps5upload's `ps5upload-core/src/fakelibs.rs` that
//! `fpkg_firmware` needs, so the converter does not pull in the whole core crate.
#![allow(dead_code)]


const SELF_MAGIC_PS4: u32 = 0x1D3D_154F;
const SELF_MAGIC_PS5: u32 = 0xEEF5_1454;
const PT_SCE_PROCPARAM: u32 = 0x6100_0001;
const PT_SCE_MODULEPARAM: u32 = 0x6100_0002;
const PROC_MAGIC: u32 = 0x4942_524F;
const MOD_MAGIC: u32 = 0x3C13_F4BF;
/// The 32-byte per-file digest a fake-signer rewrites whenever the file changes.
const DIGEST_SITE: (usize, usize) = (0x510, 32);

fn u16le(d: &[u8], at: usize) -> Option<u16> {
    d.get(at..at + 2)?.try_into().ok().map(u16::from_le_bytes)
}
fn u32le(d: &[u8], at: usize) -> Option<u32> {
    d.get(at..at + 4)?.try_into().ok().map(u32::from_le_bytes)
}
fn u64le(d: &[u8], at: usize) -> Option<u64> {
    d.get(at..at + 8)?.try_into().ok().map(u64::from_le_bytes)
}


/// Byte offset of a module's param segment, or None.
///
/// Inside a SELF the inner ELF keeps the offsets of the original unwrapped file
/// while the entry table relocates the bytes, so a segment is reached through
/// its entry (matched on id AND size) or the PT_LOAD containing it.
pub fn param_site(data: &[u8]) -> Option<usize> {
    let mut base = 0usize;
    let mut entries: Vec<(u32, u64, u64)> = Vec::new();
    if matches!(u32le(data, 0)?, SELF_MAGIC_PS4 | SELF_MAGIC_PS5) {
        let n = u16le(data, 0x18)? as usize;
        base = 0x20 + n * 0x20;
        for i in 0..n {
            let at = 0x20 + i * 0x20;
            let flags = u64le(data, at)?;
            entries.push((
                ((flags >> 20) & 0xFFF) as u32,
                u64le(data, at + 8)?,
                u64le(data, at + 16)?,
            ));
        }
    }
    let elf = data.get(base..)?;
    if elf.get(..4)? != b"\x7fELF" {
        return scan_param_site(data);
    }
    let phoff = u64le(elf, 0x20)? as usize;
    let phentsize = u16le(elf, 0x36)? as usize;
    let phnum = u16le(elf, 0x38)? as usize;
    let mut headers = Vec::with_capacity(phnum);
    for i in 0..phnum {
        let at = phoff.checked_add(i * phentsize)?;
        headers.push((u32le(elf, at)?, u64le(elf, at + 8)?, u64le(elf, at + 0x20)?));
    }
    for (i, &(ptype, off, filesz)) in headers.iter().enumerate() {
        if ptype != PT_SCE_PROCPARAM && ptype != PT_SCE_MODULEPARAM {
            continue;
        }
        let mut site = entries
            .iter()
            .find(|&&(id, _, sz)| id as usize == i && sz == filesz)
            .map(|&(_, o, _)| o as usize);
        if site.is_none() && base == 0 {
            site = Some(off as usize);
        }
        if site.is_none() {
            // A param segment often has no entry of its own and must be reached
            // through the PT_LOAD that contains it.
            for (j, &(t2, off2, sz2)) in headers.iter().enumerate() {
                if t2 == 1 && off2 <= off && off + 0x18 <= off2 + sz2 {
                    if let Some(&(_, o, _)) = entries
                        .iter()
                        .find(|&&(id, _, sz)| id as usize == j && sz == sz2)
                    {
                        site = Some((o + (off - off2)) as usize);
                        break;
                    }
                }
            }
        }
        let site = match site {
            Some(s) => s,
            None => continue,
        };
        let want = if ptype == PT_SCE_PROCPARAM {
            PROC_MAGIC
        } else {
            MOD_MAGIC
        };
        if u32le(data, site + 8) == Some(want) {
            return Some(site);
        }
    }
    scan_param_site(data)
}

/// Locate the param segment by its magic instead of by walking the ELF.
///
/// The walk fails on several real libraries — libSceAmpr, libScePlayGo and
/// friends — which is why they reported no SDK pair at all, making every
/// SDK-based decision operate on absent data. The magic occurs exactly once in
/// those files and the 0x20 size field in front of it confirms the hit, so
/// scanning is unambiguous where parsing gives up. An ambiguous or absent
/// magic returns None rather than guessing.
fn scan_param_site(data: &[u8]) -> Option<usize> {
    for magic in [PROC_MAGIC, MOD_MAGIC] {
        let pat = magic.to_le_bytes();
        let hits: Vec<usize> = data
            .windows(4)
            .enumerate()
            .filter(|(_, w)| *w == pat)
            .map(|(i, _)| i)
            .take(2)
            .collect();
        if hits.len() != 1 || hits[0] < 8 {
            continue;
        }
        let site = hits[0] - 8;
        if u32le(data, site) == Some(0x20) && site + 0x18 <= data.len() {
            return Some(site);
        }
    }
    None
}

/// Offset the param segment WOULD be at, computed from the header alone.
///
/// [`param_site`] verifies the magic at the offset it computes, which needs the
/// whole file in memory. An eboot is tens of megabytes and lives on the
/// console, so a caller that can do ranged reads wants the candidate offset
/// first, then reads 0x18 bytes there to confirm. Returns None when the walk
/// cannot place the segment; there is no magic-scan fallback here, because
/// scanning is exactly what a header-only caller is trying to avoid.
pub fn param_site_from_header(header: &[u8]) -> Option<usize> {
    let mut base = 0usize;
    let mut entries: Vec<(u32, u64, u64)> = Vec::new();
    if matches!(u32le(header, 0)?, SELF_MAGIC_PS4 | SELF_MAGIC_PS5) {
        let n = u16le(header, 0x18)? as usize;
        base = 0x20 + n * 0x20;
        for i in 0..n {
            let at = 0x20 + i * 0x20;
            let flags = u64le(header, at)?;
            entries.push((
                ((flags >> 20) & 0xFFF) as u32,
                u64le(header, at + 8)?,
                u64le(header, at + 16)?,
            ));
        }
    }
    let elf = header.get(base..)?;
    if elf.get(..4)? != b"\x7fELF" {
        return None;
    }
    let phoff = u64le(elf, 0x20)? as usize;
    let phentsize = u16le(elf, 0x36)? as usize;
    let phnum = u16le(elf, 0x38)? as usize;
    let mut headers = Vec::with_capacity(phnum);
    for i in 0..phnum {
        let at = phoff.checked_add(i * phentsize)?;
        headers.push((u32le(elf, at)?, u64le(elf, at + 8)?, u64le(elf, at + 0x20)?));
    }
    for (i, &(ptype, off, filesz)) in headers.iter().enumerate() {
        if ptype != PT_SCE_PROCPARAM && ptype != PT_SCE_MODULEPARAM {
            continue;
        }
        if let Some(&(_, o, _)) = entries
            .iter()
            .find(|&&(id, _, sz)| id as usize == i && sz == filesz)
        {
            return Some(o as usize);
        }
        if base == 0 {
            return Some(off as usize);
        }
        for (j, &(t2, off2, sz2)) in headers.iter().enumerate() {
            if t2 == 1 && off2 <= off && off + 0x18 <= off2 + sz2 {
                if let Some(&(_, o, _)) = entries
                    .iter()
                    .find(|&&(id, _, sz)| id as usize == j && sz == sz2)
                {
                    return Some((o + (off - off2)) as usize);
                }
            }
        }
    }
    None
}

/// The SDK pair in a param segment already read into `chunk` (0x18 bytes from
/// the site), verifying the magic first.
pub fn sdk_pair_at(chunk: &[u8]) -> Option<(u32, u32)> {
    let magic = u32le(chunk, 8)?;
    if magic != PROC_MAGIC && magic != MOD_MAGIC {
        return None;
    }
    Some((u32le(chunk, 0x10)?, u32le(chunk, 0x14)?))
}


/// The FW4 pair a backport targets: `(ps4, ps5)`.
pub const BACKPORT_SDK_PAIR: (u32, u32) = (0x0904_0001, 0x0400_0031);

/// Every `(ps4, ps5)` SDK pair a title can legally declare.
///
/// The pairings are NOT derivable — they are a fixed table, and writing one
/// half without the other produces a title that passes the launch gate and
/// then dies. Mirrors `payload/include/sdk_pairs.h`, which in turn matches
/// idlesauce's `ps5_elf_sdk_downgrade.py`; the core copy exists so the engine
/// can reason about a pair without asking the console.
pub const SDK_PAIRS: [(u32, u32); 10] = [
    (0x0759_0001, 0x0100_0050), // FW 1
    (0x0805_0001, 0x0200_0009), // FW 2
    (0x0854_0001, 0x0300_0027), // FW 3
    (0x0904_0001, 0x0400_0031), // FW 4 — what shipped backports use
    (0x0959_0001, 0x0500_0033), // FW 5
    (0x1009_0001, 0x0600_0038), // FW 6
    (0x1059_0001, 0x0700_0038), // FW 7
    (0x1109_0001, 0x0800_0041), // FW 8
    (0x1159_0001, 0x0900_0040), // FW 9
    (0x1209_0001, 0x1000_0040), // FW 10
];

/// Is this pair one Sony actually ships?
pub fn is_known_sdk_pair(pair: (u32, u32)) -> bool {
    SDK_PAIRS.contains(&pair)
}

/// `(ps4, ps5)` SDK words from a module's param segment, or None.
pub fn sdk_pair(data: &[u8]) -> Option<(u32, u32)> {
    let site = param_site(data)?;
    Some((u32le(data, site + 0x10)?, u32le(data, site + 0x14)?))
}
