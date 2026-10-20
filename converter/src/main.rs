//! ps5cable-convert: the game converter behind ps5cable Hub's Convert screen.
//!
//! It offers what ps5upload's Convert Games does, with the same options, by using ps5upload's
//! own writers (https://github.com/phantomptr/ps5upload, GPL-3.0-or-later):
//!
//! - `pkg`: a fake package (`.pkg`) from a decrypted game folder or a game image
//!   (compression fast / balanced / smallest / stored, content id, name, passcode, minimum
//!   firmware, PlayGo language, full verification);
//! - `image`: a game image ShadowMount+ mounts: `.ffpkg` (UFS2), `.exfat` or `.ffpfs` (PFS),
//!   optionally written straight into a compressed `.ffpfsc`, or an exFAT image with AMPR LZ4
//!   asset packs; read back and hash-checked;
//! - `folder`: a game image or debug-key package back to a plain folder;
//! - `compress`: an existing `.exfat` / `.ffpkg` image into a `.ffpfsc`;
//! - `inspect` and `estimate`: what a source is, and each compression level's size and time.
//!
//! `image_build.rs`, `folder_build.rs` and `fpkg_firmware.rs` are ps5upload's engine modules
//! at the pinned commit, unchanged but for an import; `sdk.rs` is the part of its core crate
//! they need. The HTTP job plumbing is replaced by JSON lines on stdout:
//! `{"event":"stage",...}` while working, then one `{"event":"result",...}` or
//! `{"event":"error","message":...}`. With `--cancel-on-stdin`, a `cancel` line (or the
//! parent closing stdin) stops the build between blocks and removes its partial output.

mod engine_log;
mod folder_build;
mod fpkg_api;
mod fpkg_firmware;
mod image_build;
mod sdk;

use std::collections::HashMap;
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use ps5upload_fpkg::build::{self, BuildControl, BuildRequest};
use serde_json::{json, Value};

use crate::fpkg_api::{sweep_output_leftovers, Writing};
use crate::image_build::ImageFormat;

pub(crate) fn emit(v: Value) {
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{v}");
    let _ = out.flush();
}

type Res<T> = Result<T, String>;

const USAGE: &str = "usage: ps5cable-convert <command> <source> [options]

commands:
  inspect  <source>   what the source is and whether it converts   [--output-dir D]
  estimate <source>   package size and time per compression level  [--output-dir D]
  pkg      <source>   build a fake package (.pkg)
           --output-dir D  --content-id ID  --name STEM  --passcode P  --firmware 5.10
           --compression fast|balanced|smallest|stored  --language de-DE  --full-verify
  image    <folder>   build a game image ShadowMount+ mounts
           --output-dir D  --format ffpkg|exfat|ffpfs  --compress (.ffpfsc)
           --lz4 [--lz4-level 1-12 --lz4-block-kib 16-1024 --lz4-profile FILE.toml
                  --lz4-install-runtime --lz4-runtime FILE]
           --verify-chunk-kib 1024|4096|8192|16384|32768  --full-verify
  folder   <source>   unpack an image or debug-key package to a folder
           --output-dir D  --name NAME  --full-verify
  compress <image>    compress an .exfat/.ffpkg image into a .ffpfsc
           --output-dir D  --level 1-9  --full-verify
  runtime-info        where the ampr_emu runtime for --lz4-install-runtime comes from

every command: --cancel-on-stdin";

struct Args {
    cmd: String,
    source: Option<String>,
    opts: HashMap<String, String>,
    flags: Vec<String>,
}

const FLAGS: &[&str] = &[
    "full-verify",
    "compress",
    "lz4",
    "lz4-install-runtime",
    "cancel-on-stdin",
];

impl Args {
    fn parse() -> Res<Args> {
        let mut it = std::env::args().skip(1);
        let cmd = it.next().ok_or_else(|| USAGE.to_string())?;
        let (mut source, mut opts, mut flags) = (None, HashMap::new(), Vec::new());
        while let Some(a) = it.next() {
            if let Some(key) = a.strip_prefix("--") {
                if FLAGS.contains(&key) {
                    flags.push(key.to_string());
                } else {
                    let v = it.next().ok_or_else(|| format!("--{key} needs a value"))?;
                    opts.insert(key.to_string(), v);
                }
            } else if source.is_none() {
                source = Some(a);
            } else {
                return Err(format!("unexpected argument {a:?}\n\n{USAGE}"));
            }
        }
        Ok(Args { cmd, source, opts, flags })
    }

    fn get(&self, k: &str) -> Option<&str> {
        self.opts.get(k).map(String::as_str).filter(|v| !v.trim().is_empty())
    }

    fn flag(&self, k: &str) -> bool {
        self.flags.iter().any(|f| f == k)
    }

    fn source(&self) -> Res<PathBuf> {
        let raw = self.source.as_deref().ok_or_else(|| format!("{} needs a source\n\n{USAGE}", self.cmd))?;
        resolve(raw)
    }

    fn output_dir(&self) -> Res<PathBuf> {
        match self.get("output-dir") {
            Some(d) => resolve(d),
            None => Ok(default_output_dir()),
        }
    }

    fn num<T: std::str::FromStr>(&self, k: &str) -> Res<Option<T>> {
        self.get(k)
            .map(|v| v.trim().parse::<T>().map_err(|_| format!("--{k}: {v:?} is not a number")))
            .transpose()
    }
}

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
}

fn default_output_dir() -> PathBuf {
    home().join("Downloads").join("fpkgs")
}

fn resolve(raw: &str) -> Res<PathBuf> {
    let raw = raw.trim();
    let p = if raw == "~" {
        home()
    } else if let Some(rest) = raw.strip_prefix("~/") {
        home().join(rest)
    } else {
        PathBuf::from(raw)
    };
    if p.is_absolute() {
        Ok(p)
    } else {
        Ok(std::env::current_dir().map_err(|e| e.to_string())?.join(p))
    }
}

/// Stage progress at most five times a second, and always at a stage's start and end.
struct Progress {
    last: Instant,
    stage: String,
}

impl Progress {
    fn new() -> Self {
        Progress { last: Instant::now() - Duration::from_secs(1), stage: String::new() }
    }

    fn report(&mut self, id: &str, index: usize, count: usize, done: u64, total: u64) {
        let changed = id != self.stage;
        if !changed && self.last.elapsed() < Duration::from_millis(200) && done < total {
            return;
        }
        self.last = Instant::now();
        self.stage = id.to_string();
        emit(json!({ "event": "stage", "id": id, "index": index, "count": count,
                     "done": done, "total": total }));
    }
}

fn cancel_flag(args: &Args) -> Arc<AtomicBool> {
    let cancel = Arc::new(AtomicBool::new(false));
    if args.flag("cancel-on-stdin") {
        let c = cancel.clone();
        std::thread::spawn(move || {
            let stdin = std::io::stdin();
            for line in stdin.lock().lines() {
                match line {
                    Ok(l) if l.trim() == "cancel" => break,
                    Ok(_) => continue,
                    Err(_) => break,
                }
            }
            c.store(true, Ordering::Relaxed);
        });
    }
    cancel
}

fn min_firmware_of(source: &Path, declared: Option<&str>) -> Option<String> {
    let mut tree = ps5upload_fpkg::source::open(source).ok()?;
    fpkg_firmware::min_firmware(tree.as_mut(), declared)
}

// ----------------------------------------------------------------------------- commands

fn cmd_inspect(args: &Args) -> Res<Value> {
    let source = args.source()?;
    let out = args.output_dir()?;
    let inspection = build::inspect(&source, &out).map_err(|e| e.to_string())?;
    let min_firmware = min_firmware_of(&source, inspection.required_firmware.as_deref());
    let mut v = serde_json::to_value(&inspection).map_err(|e| e.to_string())?;
    v["min_firmware"] = json!(min_firmware);
    v["is_folder"] = json!(source.is_dir());
    v["is_image"] = json!(image_build::is_image_file(&source));
    Ok(v)
}

fn cmd_estimate(args: &Args) -> Res<Value> {
    let source = args.source()?;
    let out = args.output_dir()?;
    let e = build::estimate(&source, Some(&out)).map_err(|e| e.to_string())?;
    serde_json::to_value(e).map_err(|e| e.to_string())
}

const PKG_STAGES: [build::Stage; 5] = [
    build::Stage::Check,
    build::Stage::Plan,
    build::Stage::Compress,
    build::Stage::Write,
    build::Stage::Verify,
];

fn cmd_pkg(args: &Args, cancel: &AtomicBool) -> Res<Value> {
    let source = args.source()?;
    let out = args.output_dir()?;
    let inspection = build::inspect(&source, &out).map_err(|e| e.to_string())?;
    let stem = args
        .get("name")
        .or_else(|| args.get("content-id"))
        .map(str::to_string)
        .or_else(|| inspection.content_id.clone());
    let _partial = match stem.map(|stem| out.join(format!("{stem}.pkg.partial"))) {
        Some(path) => Some(Writing::claim(path.clone()).ok_or_else(|| format!("{} is already being built", path.display()))?),
        None => None,
    };
    sweep_output_leftovers(&out);

    let mut request = BuildRequest::new(&source, &out);
    request.content_id = args.get("content-id").map(str::to_string);
    request.file_name = args.get("name").map(str::to_string);
    if let Some(p) = args.opts.get("passcode").filter(|p| !p.is_empty()) {
        request.passcode = p.clone();
    }
    request.firmware = args.get("firmware").map(str::to_string);
    if request.firmware.is_none() {
        request.firmware = min_firmware_of(&source, inspection.required_firmware.as_deref());
    }
    request.language = args.get("language").map(|l| l.trim().to_string());
    if let Some(level) = args.get("compression") {
        request.level = level.parse()?;
    }

    let progress = std::cell::RefCell::new((Progress::new(), u32::MAX));
    let mut on_stage = |s: build::Stage| {
        let mut p = progress.borrow_mut();
        p.1 = s.index();
        p.0.report(s.id(), s.index() as usize, build::STAGE_COUNT as usize, 0, 0);
    };
    let mut on_bytes = |done: u64, total: u64| {
        let mut p = progress.borrow_mut();
        let at = p.1;
        if let Some(s) = PKG_STAGES.get(at as usize) {
            p.0.report(s.id(), at as usize, build::STAGE_COUNT as usize, done, total);
        }
    };
    let mut control = BuildControl {
        bytes: Some(&mut on_bytes),
        cancel: Some(cancel),
        stage: Some(&mut on_stage),
        sample: (!args.flag("full-verify")).then(ps5upload_fpkg::sample::fresh_seed),
    };
    let mut phase = |line: &str| engine_log::record("info", format!("fpkg: {line}"));
    let outcome = build::build_controlled(&request, &mut phase, &mut control);
    if outcome.is_err() {
        sweep_output_leftovers(&out);
    }
    let report = outcome.map_err(|e| if cancel.load(Ordering::Relaxed) { "cancelled".into() } else { e.to_string() })?;
    Ok(json!({
        "path": report.path, "size": report.size, "content_id": report.content_id,
        "warnings": report.warnings.iter().map(|w| w.to_string()).collect::<Vec<_>>(),
        "firmware": request.firmware, "files": inspection.files,
    }))
}

fn lz4_options(args: &Args) -> Res<image_build::Lz4Packs> {
    let level = args.num::<u8>("lz4-level")?.unwrap_or(9);
    if !(1..=12).contains(&level) {
        return Err(format!("LZ4 level {level} is not 1 to 12"));
    }
    let block = u64::from(args.num::<u32>("lz4-block-kib")?.unwrap_or(64)) * 1024;
    let block_shift = ps5upload_fpkg::ampr_pack::config::block_shift(block).map_err(|e| e.to_string())?;
    let profile = match args.get("lz4-profile") {
        Some(file) => {
            let text = std::fs::read_to_string(resolve(file)?).map_err(|e| format!("{file}: {e}"))?;
            Some(ps5upload_fpkg::ampr_pack::Config::from_toml(&text, None).map_err(|e| format!("the LZ4 profile: {e}"))?)
        }
        None => None,
    };
    Ok(image_build::Lz4Packs { profile, level, block_shift })
}

/// The ampr_emu release, from a file the caller fetched, used only when its SHA-256 matches.
fn release_runtime(args: &Args) -> Res<Vec<u8>> {
    use ps5upload_fpkg::ampr_pack::image as img;
    use sha2::Digest;
    let file = args.get("lz4-runtime").ok_or_else(|| {
        format!("--lz4-install-runtime needs --lz4-runtime: download {} first", img::RUNTIME_URL)
    })?;
    let bytes = std::fs::read(resolve(file)?).map_err(|e| format!("{file}: {e}"))?;
    let hex: String = sha2::Sha256::digest(&bytes).iter().map(|b| format!("{b:02x}")).collect();
    if hex != img::RUNTIME_SHA256 {
        return Err("the ampr_emu runtime file does not match the expected digest; not used".into());
    }
    Ok(bytes)
}

fn cmd_image(args: &Args, cancel: &AtomicBool) -> Res<Value> {
    let source = args.source()?;
    let image_file = image_build::is_image_file(&source);
    if !image_file && !source.is_dir() {
        return Err(format!("{} is not a game folder or image", source.display()));
    }
    let format = ImageFormat::parse(args.get("format"))?;
    let compress = args.flag("compress");
    let verify_chunk = image_build::verify_chunk(args.num::<u32>("verify-chunk-kib")?)?;
    // A fast read-back also covers UFS2's indirect-block seam.
    let mode = ps5upload_fpkg::sample::VerifyMode::from_full(args.flag("full-verify"), format == ImageFormat::Ffpkg);
    let packs = if args.flag("lz4") {
        if format != ImageFormat::Exfat || compress {
            return Err("LZ4 asset packs go into a plain exFAT image: choose exFAT and turn off .ffpfsc compression".into());
        }
        Some(lz4_options(args)?)
    } else {
        None
    };
    let out_dir = args.output_dir()?;
    let (out_name, inner_name) = image_build::output_names(&source, format, compress);
    let output = out_dir.join(out_name);
    if image_file && std::fs::canonicalize(&output).ok() == std::fs::canonicalize(&source).ok() {
        return Err("the image would be written over its own source: pick another format or output folder".into());
    }
    if output.exists() {
        return Err(format!("{} already exists", output.display()));
    }
    std::fs::create_dir_all(&out_dir).map_err(|e| format!("{}: {e}", out_dir.display()))?;

    // A UFS2 image (.ffpkg) is case-sensitive like /data: give modules the eboot's spelling.
    let (mut tree, renamed) = ps5upload_fpkg::module_case::open(&source).map_err(|e| e.to_string())?;
    if !renamed.is_empty() {
        log_info!("image build: renamed to the eboot's spelling: {}", ps5upload_fpkg::module_case::describe(&renamed));
    }
    if packs.is_some() && args.flag("lz4-install-runtime") {
        let ready = ps5upload_fpkg::ampr_pack::image::readiness(&mut *tree);
        match ready.as_ref().map(|r| r.kind) {
            Some("runtime") => {
                tree = Box::new(
                    ps5upload_fpkg::ampr_pack::image::RuntimeInstalled::new(tree, release_runtime(args)?)
                        .map_err(|e| e.to_string())?,
                );
            }
            Some("ok") => {}
            _ => return Err(ready.and_then(|r| r.refusal).unwrap_or_else(|| "the game cannot use asset packs".into())),
        }
    }
    if packs.is_some() {
        if let Some(why) = ps5upload_fpkg::ampr_pack::image::refusal(&mut *tree) {
            return Err(why);
        }
    }
    if tree.files().is_empty() {
        return Err(format!("{} has no files in it", source.display()));
    }

    let partial = PathBuf::from(format!("{}.partial", output.display()));
    let _writing = Writing::claim(partial.clone()).ok_or_else(|| format!("{} is already being built", partial.display()))?;
    let spool = image_build::lz4_spool(&output);
    let _spooling = Writing::claim(spool.clone()).ok_or_else(|| format!("{} is already being built", spool.display()))?;
    sweep_output_leftovers(&out_dir);

    let stages: &[&str] = if packs.is_some() { &["pack", "plan", "write", "verify"] } else { &["plan", "write", "verify"] };
    let mut progress = Progress::new();
    let mut report = |id: &str, done: u64, total: u64| {
        let index = stages.iter().position(|s| *s == id || (id == "compress" && *s == "write")).unwrap_or(0);
        progress.report(id, index, stages.len(), done, total);
    };
    let outcome = match &packs {
        Some(packs) => image_build::build_packed(format, &mut *tree, &output, packs, mode, verify_chunk, cancel, &mut report),
        // A libSceAmpr game gets a fresh ampr_emu.index, as a package does.
        None => match image_build::AmprIndexed::wrap(&mut *tree) {
            Ok(Some(mut indexed)) => image_build::build(format, inner_name.as_deref(), &mut indexed, &output, mode, verify_chunk, cancel, &mut report),
            Ok(None) => image_build::build(format, inner_name.as_deref(), &mut *tree, &output, mode, verify_chunk, cancel, &mut report),
            Err(e) => Err(format!("ampr_emu.index: {e}")),
        },
    };
    if outcome.is_err() {
        sweep_output_leftovers(&out_dir);
    }
    let built = outcome?;
    Ok(json!({ "path": output, "size": built.image_bytes, "files": built.files, "detail": built.detail }))
}

fn cmd_folder(args: &Args, cancel: &AtomicBool) -> Res<Value> {
    let source = args.source()?;
    if source.is_dir() {
        return Err(format!("{} is already a folder", source.display()));
    }
    if !image_build::is_image_file(&source) {
        return Err(format!("{} is not a game image or package", source.display()));
    }
    let name = match args.get("name").map(str::trim) {
        Some(n) => {
            if let Some(why) = folder_build::name_problem(n) {
                return Err(format!("the folder name {n:?} {why}"));
            }
            n.to_string()
        }
        None => image_build::stem_of(&source),
    };
    let out_dir = args.output_dir()?;
    let target = out_dir.join(&name);
    if target.exists() {
        return Err(format!("{} already exists", target.display()));
    }
    let partial = folder_build::partial_of(&target);
    if partial.exists() && !folder_build::is_our_partial_dir(&partial) {
        return Err(format!("{} already exists", partial.display()));
    }
    std::fs::create_dir_all(&out_dir).map_err(|e| format!("{}: {e}", out_dir.display()))?;
    // An image ignores letter case and /data does not: the folder takes the eboot's spelling.
    let (mut tree, renamed) = ps5upload_fpkg::module_case::open(&source).map_err(|e| e.to_string())?;
    if !renamed.is_empty() {
        log_info!("folder build: renamed to the eboot's spelling: {}", ps5upload_fpkg::module_case::describe(&renamed));
    }
    if tree.files().is_empty() {
        return Err(format!("{} has no files in it", source.display()));
    }
    let total: u64 = tree.files().iter().map(|f| f.size).sum();
    if let Some(why) = build::free_bytes(&out_dir).and_then(|free| folder_build::shortfall(free, total, &out_dir)) {
        return Err(why);
    }
    let _writing = Writing::claim(partial.clone()).ok_or_else(|| format!("{} is already being built", partial.display()))?;
    sweep_output_leftovers(&out_dir);
    let mode = ps5upload_fpkg::sample::VerifyMode::from_full(args.flag("full-verify"), false);
    const STAGES: [&str; 2] = ["write", "verify"];
    let mut progress = Progress::new();
    let mut report = |id: &str, done: u64, total: u64| {
        let index = STAGES.iter().position(|s| *s == id).unwrap_or(0);
        progress.report(id, index, STAGES.len(), done, total);
    };
    let reopen_source = source.clone();
    let reopen = move || ps5upload_fpkg::module_case::open(&reopen_source).map(|(t, _)| t).map_err(|e| e.to_string());
    let outcome = folder_build::build(&mut *tree, &reopen, &target, mode, cancel, &mut report);
    drop(tree);
    if outcome.is_err() {
        sweep_output_leftovers(&out_dir);
    }
    let built = outcome?;
    Ok(json!({ "path": target, "size": built.bytes, "files": built.files, "detail": built.detail }))
}

fn cmd_compress(args: &Args, cancel: &AtomicBool) -> Res<Value> {
    use ps5upload_fpkg::ffpfsc;
    let source = args.source()?;
    match std::fs::metadata(&source) {
        Ok(m) if m.is_file() && m.len() > 0 => {}
        Ok(_) => return Err(format!("{} is not a game image file", source.display())),
        Err(e) => return Err(format!("{}: {e}", source.display())),
    }
    let out_dir = match args.get("output-dir") {
        Some(d) => resolve(d)?,
        None => source.parent().map(Path::to_path_buf).unwrap_or_else(default_output_dir),
    };
    let stem = source.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_else(|| "image".into());
    let output = out_dir.join(format!("{stem}.ffpfsc"));
    if output.exists() {
        return Err(format!("{} already exists", output.display()));
    }
    std::fs::create_dir_all(&out_dir).map_err(|e| format!("{}: {e}", out_dir.display()))?;
    let partial = PathBuf::from(format!("{}.partial", output.display()));
    let _writing = Writing::claim(partial.clone()).ok_or_else(|| format!("{} is already being built", partial.display()))?;
    sweep_output_leftovers(&out_dir);
    let mut options = ffpfsc::WrapOptions::default();
    if let Some(level) = args.num::<u32>("level")? {
        options.level = level;
    }
    options.sample = (!args.flag("full-verify")).then(ps5upload_fpkg::sample::fresh_seed);
    let mut p = Progress::new();
    let mut on_progress = |done: u64, total: u64| p.report("compress", 0, 1, done, total);
    let mut control = ffpfsc::Control { progress: Some(&mut on_progress), cancel: Some(cancel) };
    let outcome = ffpfsc::wrap(&source, &output, &options, &mut control);
    if outcome.is_err() {
        sweep_output_leftovers(&out_dir);
    }
    let r = outcome.map_err(|e| if cancel.load(Ordering::Relaxed) { "cancelled".into() } else { e.to_string() })?;
    Ok(json!({
        "path": r.output, "size": r.stored_size, "raw_size": r.raw_size, "compressed": r.compressed,
        "detail": format!("{}/{} blocks compressed, inner name {}", r.compressed_blocks, r.blocks, r.inner_name),
    }))
}

fn cmd_runtime_info() -> Res<Value> {
    use ps5upload_fpkg::ampr_pack::image as img;
    Ok(json!({ "url": img::RUNTIME_URL, "sha256": img::RUNTIME_SHA256 }))
}

fn main() {
    let args = match Args::parse() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    };
    let cancel = cancel_flag(&args);
    let started = Instant::now();
    let result = match args.cmd.as_str() {
        "inspect" => cmd_inspect(&args),
        "estimate" => cmd_estimate(&args),
        "pkg" => cmd_pkg(&args, &cancel),
        "image" => cmd_image(&args, &cancel),
        "folder" => cmd_folder(&args, &cancel),
        "compress" => cmd_compress(&args, &cancel),
        "runtime-info" => cmd_runtime_info(),
        "-h" | "--help" | "help" => {
            println!("{USAGE}");
            return;
        }
        other => Err(format!("unknown command {other:?}\n\n{USAGE}")),
    };
    match result {
        Ok(v) => emit(json!({ "event": "result", "elapsed_ms": started.elapsed().as_millis() as u64, "result": v })),
        Err(e) => {
            let e = if cancel.load(Ordering::Relaxed) && e != "cancelled" { format!("cancelled ({e})") } else { e };
            emit(json!({ "event": "error", "message": e }));
            std::process::exit(1);
        }
    }
}
