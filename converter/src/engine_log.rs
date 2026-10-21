//! Where ps5upload's engine writes its log, the converter writes JSON lines on stdout, which
//! the Hub keeps with the job.

pub(crate) fn record(level: &str, message: impl Into<String>) {
    crate::emit(serde_json::json!({ "event": "log", "level": level, "message": message.into() }));
}

#[macro_export]
macro_rules! log_info {
    ($($arg:tt)*) => { $crate::engine_log::record("info", format!($($arg)*)) };
}
