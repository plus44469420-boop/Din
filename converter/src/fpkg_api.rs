//! The output-folder bookkeeping of ps5upload's `fpkg_api.rs`: which `.partial` files a build is
//! writing, and the sweep that clears what an interrupted build left behind. The Hub runs one
//! conversion at a time, so a single process only has to know about its own.

use std::path::{Path, PathBuf};
use std::time::Duration;

fn active_partials() -> &'static std::sync::Mutex<std::collections::HashSet<PathBuf>> {
    static SET: std::sync::OnceLock<std::sync::Mutex<std::collections::HashSet<PathBuf>>> =
        std::sync::OnceLock::new();
    SET.get_or_init(Default::default)
}

/// Marks a file as being written for as long as it lives: the sweep leaves it alone.
pub(crate) struct Writing(PathBuf);

impl Writing {
    /// Claims `path`, removing a leftover of an earlier run that died. `None` when it is
    /// already claimed in this process.
    pub(crate) fn claim(path: PathBuf) -> Option<Writing> {
        let mut active = active_partials().lock().unwrap_or_else(|e| e.into_inner());
        if active.contains(&path) {
            return None;
        }
        if path.is_dir() {
            let _ = std::fs::remove_dir_all(&path);
        } else if path.is_file() {
            let _ = std::fs::remove_file(&path);
        }
        active.insert(path.clone());
        Some(Writing(path))
    }
}

impl Drop for Writing {
    fn drop(&mut self) {
        active_partials()
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&self.0);
    }
}

fn is_our_partial(name: &str) -> bool {
    [
        ".pkg.partial",
        ".ffpfsc.partial",
        ".ffpfs.partial",
        ".exfat.partial",
        ".ffpkg.partial",
    ]
    .iter()
    .any(|ext| name.to_ascii_lowercase().ends_with(ext))
}

/// Removes a file, retrying while something (an antivirus, the indexer) holds it open.
fn remove_file_retrying(path: &Path) {
    for attempt in 0..20 {
        match std::fs::remove_file(path) {
            Ok(()) => return,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return,
            Err(e) if attempt == 19 => {
                crate::engine_log::record("warn", format!("could not remove {}: {e}", path.display()));
            }
            Err(_) => std::thread::sleep(Duration::from_millis(250)),
        }
    }
}

/// Removes what failed or interrupted conversions left in `out`: build `.partial` files, LZ4
/// pack spools and unfinished folders, but nothing this process is writing.
pub(crate) fn sweep_output_leftovers(out: &Path) {
    let Ok(entries) = std::fs::read_dir(out) else {
        return;
    };
    let (stale, dirs): (Vec<PathBuf>, Vec<PathBuf>) = {
        let active = active_partials().lock().unwrap_or_else(|e| e.into_inner());
        entries
            .flatten()
            .filter(|e| {
                let name = e.file_name().to_string_lossy().to_ascii_lowercase();
                match e.file_type() {
                    Ok(t) if t.is_file() => is_our_partial(&name),
                    Ok(t) if t.is_dir() => {
                        name.ends_with(".exfat.lz4spool")
                            || crate::folder_build::is_our_partial_dir(&e.path())
                    }
                    _ => false,
                }
            })
            .map(|e| e.path())
            .filter(|p| !active.contains(p))
            .partition(|p| p.is_file())
    };
    for p in stale {
        remove_file_retrying(&p);
    }
    for p in dirs {
        let _ = std::fs::remove_dir_all(&p);
    }
}
