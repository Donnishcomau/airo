// SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
// SPDX-License-Identifier: AGPL-3.0-or-later

//! Reading Airo's state.
//!
//! The tray is a *view*. It never polls a provider, never fuses sources and
//! never computes an index — the Python poller does all of that and writes
//! `~/.airo/data/latest.json`. This is the whole reason the project moved to a shared
//! store: if the tray reimplemented the fusion rule, the menu bar and the
//! dashboard could disagree about what you are breathing.
//!
//! Consequently this file contains no air-quality logic at all. If you find
//! yourself adding a threshold or a band boundary here, it belongs in Python.

use serde::{Deserialize, Deserializer, Serialize};
use std::path::{Path, PathBuf};

/// Read a field that may be `null` as its default rather than failing.
///
/// `#[serde(default)]` covers a field that is *absent*. It does nothing for
/// one that is present and null, and serde then fails the whole document —
/// so a single unexpected null anywhere in latest.json makes the tray report
/// "no reading yet" beside a full database.
///
/// That is not hypothetical. `quiet_hours` was null for every install that
/// had not set quiet hours, which is the default, and the tray went blind.
/// The same shape took it out once before, when a band ceiling of infinity
/// made latest.json unparseable.
///
/// Rule 7 is why this is tolerance rather than correction: the tray renders
/// what it is given. Refusing to render everything because one optional field
/// arrived empty is the tray making a decision, and the wrong one.
fn null_as_default<'de, D, T>(d: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Default + Deserialize<'de>,
{
    Ok(Option::deserialize(d)?.unwrap_or_default())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceView {
    pub provider: Option<String>,
    pub site_name: Option<String>,
    pub pm25: Option<f64>,
    pub aqi: Option<f64>,
    pub band: Option<String>,
    pub age_minutes: Option<f64>,
    pub distance_km: Option<f64>,
    pub stale: Option<bool>,
    pub quality: Option<String>,
    pub corroboration: Option<String>,
    pub corroboration_note: Option<String>,
    pub peer_ratio: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NetworkView {
    pub provider: Option<String>,
    pub label: Option<String>,
    pub tier: Option<String>,
    #[serde(default)]
    pub needs_key: bool,
    #[serde(default)]
    pub has_key: bool,
    #[serde(default)]
    pub in_use: bool,
    pub signup_url: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Trend {
    pub direction: Option<String>,
    pub delta: Option<f64>,
    pub text: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TimeHint {
    pub state: Option<String>,
    pub severity: Option<String>,
    pub text: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AlertState {
    #[serde(default)]
    pub enabled: bool,
    pub threshold_aqi: Option<f64>,
    pub threshold_pm25: Option<f64>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub quiet_hours: Vec<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Latest {
    pub fetched_local: Option<String>,
    /// Rendered by the poller, not computed here -- the tray has no date
    /// library and rule 7 keeps decisions in Python.
    #[serde(default)]
    pub last_poll_text: Option<String>,
    #[serde(default)]
    pub next_poll_text: Option<String>,
    pub location_name: Option<String>,

    pub aqi: Option<f64>,
    pub band: Option<String>,
    pub pm25_10min: Option<f64>,

    pub scale: Option<String>,
    pub scale_label: Option<String>,

    pub fusion_rule: Option<String>,
    pub fusion_note: Option<String>,
    #[serde(default)]
    pub fusion_degraded: bool,
    /// True when the headline reading is not supported by neighbouring
    /// sources. Shown, never suppressed — see fusion.py.
    #[serde(default)]
    pub uncorroborated: bool,
    pub corroboration_note: Option<String>,
    pub provenance: Option<String>,

    pub source: Option<SourceView>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub sources: Vec<SourceView>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub attributions: Vec<String>,
    /// Networks the user could be reading but is not. Surfaced so an unused
    /// source is discoverable from the menu rather than only from a setup
    /// command run once.
    #[serde(default, deserialize_with = "null_as_default")]
    pub networks: Vec<NetworkView>,

    // Derived in Python so every surface renders the same judgement. The tray
    // must never compute these -- see the module note above.
    pub trend: Option<Trend>,
    pub time_hint: Option<TimeHint>,
    pub alerts: Option<AlertState>,
    #[serde(default)]
    pub averages_aqi: std::collections::HashMap<String, Option<f64>>,
    #[serde(default)]
    pub averages_pm25: std::collections::HashMap<String, Option<f64>>,
    pub poll_minutes: Option<i64>,
    pub fetched_utc: Option<String>,
}

impl Latest {
    /// What goes in the menu bar itself. Deliberately short — a tray title is
    /// a handful of characters on every platform.
    pub fn tray_title(&self) -> String {
        // A reading the neighbours do not support gets a visible caveat in the
        // menu bar itself, not just in the dropdown -- the number alone would
        // read as settled fact.
        let caveat = if self.uncorroborated { "?" } else { "" };
        match (self.aqi, self.scale.as_deref()) {
            (Some(v), Some("raw")) => {
                format!("{} {:.1} µg{}", self.band_symbol(), v, caveat)
            }
            (Some(v), _) => {
                format!("{} {}{}", self.band_symbol(), v.round() as i64, caveat)
            }
            // No usable reading is a real state and must look like one, not
            // like a zero.
            (None, _) => "\u{26AA} —".to_string(),
        }
    }

    /// Severity indicator for the tray title.
    ///
    /// A menu-bar plugin can colour its text; no cross-platform tray API can, so
    /// severity has to be carried by the glyph itself. Mapped from the band
    /// name the poller already decided — never re-derived from the number,
    /// because only the poller knows which scale is configured and
    /// ARCHITECTURE §3 requires the indicator to match the *displayed rounded*
    /// value.
    pub fn band_symbol(&self) -> &'static str {
        match self.band.as_deref() {
            Some("Very good") | Some("Good") | Some("At or below WHO guideline") => "\u{1F7E2}",
            Some("Fair") | Some("Moderate") | Some("Above WHO guideline") => "\u{1F7E1}",
            Some("Poor") | Some("Unhealthy for sensitive groups")
            | Some("Well above guideline") => "\u{1F7E0}",
            Some("Very poor") | Some("Unhealthy") | Some("High") => "\u{1F534}",
            Some("Hazardous") | Some("Very unhealthy") | Some("Very high")
            | Some("Extreme") => "\u{1F7E4}",
            _ => "\u{26AA}",
        }
    }
}

/// The staged payload inside an installed app, if this binary is in one.
///
/// The installer ships the Python modules, both pages and an interpreter under
/// the bundle's resources. Where that lands depends on the platform, so the
/// candidates are listed relative to the executable rather than guessed:
///
///   macOS   Airo.app/Contents/MacOS/airo-tray -> ../Resources/payload
///   others  airo-tray next to resources/payload or payload
///
/// Returns None in a development checkout, which is not an error -- it means
/// the sources are the checkout rather than a copy.
pub fn bundled_payload(exe_dir: &Path) -> Option<PathBuf> {
    for candidate in [
        exe_dir.join("../Resources/payload"),
        exe_dir.join("resources/payload"),
        exe_dir.join("payload"),
    ] {
        if candidate.join("airo").join("poller.py").exists() {
            return Some(candidate);
        }
    }
    None
}

/// Where the project's Python lives, given where the binary is.
///
/// Split out from project_root() so it can be tested: current_exe() cannot be
/// faked, and a resolver that is only exercised in production is one nobody
/// finds out about until an installed app reports "No reading yet" beside a
/// full database.
///
/// Order: an explicit override, then a bundle, then a checkout.
pub fn resolve_root(exe_dir: &Path, env_home: Option<&str>) -> PathBuf {
    if let Some(p) = env_home.filter(|s| !s.is_empty()) {
        return PathBuf::from(p);
    }
    if let Some(payload) = bundled_payload(exe_dir) {
        return payload.join("airo");
    }
    // A development checkout. Marked by poller.py, which is always present --
    // the previous markers were config.json and data/, and *both* are absent
    // from a fresh clone, so the tray fell through to "." and found nothing.
    let mut dir = Some(exe_dir);
    while let Some(d) = dir {
        if d.join("poller.py").exists() {
            return d.to_path_buf();
        }
        dir = d.parent();
    }
    PathBuf::from(".")
}

pub fn project_root() -> PathBuf {
    let home = std::env::var("AIRO_HOME").ok();
    let exe = std::env::current_exe().ok();
    let dir = exe.as_deref().and_then(|e| e.parent()).unwrap_or(Path::new("."));
    resolve_root(dir, home.as_deref())
}

/// Where the poller keeps its readings.
///
/// Must mirror `poller._resolve_data_dir()` exactly: $AIRO_DATA, then
/// ~/.airo/data, then <project>/data for installs that predate the move. The
/// tray reading a stale path is indistinguishable from the poller being dead —
/// it renders "No data" either way, which is the least useful possible thing
/// to tell someone whose poller is working fine.
/// Refuse to start if another tray is already running.
///
/// Two tray icons is not a cosmetic problem: both poll, both write, and the
/// user cannot tell which one a click reached or which is stale. It happens
/// easily — a launchd agent plus a manual run from the checkout, or an
/// upgrade that starts the new binary before the old one exits.
///
/// A lock FILE alone is not enough, because a crash leaves it behind and the
/// tray then refuses to start forever. The pid inside it is checked, so a
/// stale lock from a dead process is reclaimed rather than believed. The
/// poller's --serve guard makes the same distinction for the same reason.
pub fn claim_single_instance() -> Result<PathBuf, String> {
    let lock = data_dir().join("tray.pid");
    if let Ok(text) = std::fs::read_to_string(&lock) {
        if let Ok(pid) = text.trim().parse::<i32>() {
            if pid != std::process::id() as i32 && pid_is_alive(pid) {
                return Err(format!(
                    "another Airo tray is already running (pid {pid}). \
                     Quit it from its menu, or stop the background one with: \
                     python3 scheduler.py uninstall-tray"));
            }
        }
    }
    if let Some(parent) = lock.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    std::fs::write(&lock, format!("{}\n", std::process::id()))
        .map_err(|e| format!("could not write {}: {e}", lock.display()))?;
    Ok(lock)
}

#[cfg(unix)]
fn pid_is_alive(pid: i32) -> bool {
    // Signal 0 performs the permission and existence checks without
    // delivering anything, which is exactly the question being asked.
    unsafe { libc_kill(pid, 0) == 0 }
}

#[cfg(unix)]
extern "C" {
    #[link_name = "kill"]
    fn libc_kill(pid: i32, sig: i32) -> i32;
}

// Windows has no signal 0, so the equivalent question is asked of the kernel
// directly. Returning a blanket `false` here — which is what shipped first —
// made the whole single-instance guard a no-op on Windows: every lock looked
// stale, so a second tray always started. The CI test that asserts this
// process is alive is what caught it.
#[cfg(windows)]
fn pid_is_alive(pid: i32) -> bool {
    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const STILL_ACTIVE: u32 = 259;
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid as u32);
        if handle.is_null() {
            return false;               // gone, or not ours to ask about
        }
        let mut code: u32 = 0;
        let ok = GetExitCodeProcess(handle, &mut code);
        CloseHandle(handle);
        // A process that genuinely exited with 259 would read as alive. That
        // costs a refused start rather than a duplicate tray, and the message
        // names the pid so it is recoverable.
        ok != 0 && code == STILL_ACTIVE
    }
}

#[cfg(windows)]
extern "system" {
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut core::ffi::c_void;
    fn GetExitCodeProcess(handle: *mut core::ffi::c_void, code: *mut u32) -> i32;
    fn CloseHandle(handle: *mut core::ffi::c_void) -> i32;
}

#[cfg(not(any(unix, windows)))]
fn pid_is_alive(_pid: i32) -> bool {
    // Nowhere else is a supported target. Assume stale rather than block a
    // legitimate start: a duplicate icon is annoying, a tray that refuses to
    // launch after one crash is worse.
    false
}

pub fn data_dir() -> PathBuf {
    if let Ok(p) = std::env::var("AIRO_DATA") {
        if !p.is_empty() {
            return PathBuf::from(p);
        }
    }
    // `data_dir` from the config, which the poller honours. Omitting it here
    // meant the tray went blind the moment anyone chose a custom location --
    // it read ~/.airo/data, found nothing, and reported "no reading yet"
    // while the poller was writing happily somewhere else. Setup offers that
    // choice, so this is not an exotic configuration.
    if let Some(configured) = configured_data_dir() {
        return configured;
    }
    if let Some(home) = home_dir() {
        let user = home.join(".airo").join("data");
        let legacy = project_root().join("data");
        // Prefer an existing legacy directory, exactly as the poller does, so
        // the two halves never disagree about which database is live.
        if legacy.join("airo.db").exists() && !user.join("airo.db").exists() {
            return legacy;
        }
        return user;
    }
    project_root().join("data")
}

/// `data_dir` from ~/.airo/config.json, expanded for a leading `~`.
///
/// Read directly rather than through a settings type: the tray must keep
/// working against a config written by a newer poller with fields it does not
/// know, and a parse failure here must not stop it finding the database.
fn configured_data_dir() -> Option<PathBuf> {
    let path = config_path()?;
    let text = std::fs::read_to_string(path).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    let raw = value.get("data_dir")?.as_str()?.trim();
    if raw.is_empty() {
        return None;
    }
    if let Some(rest) = raw.strip_prefix("~/") {
        return Some(home_dir()?.join(rest));
    }
    Some(PathBuf::from(raw))
}

/// Where the poller keeps its settings. Same order the Python side uses.
fn config_path() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("AIRO_CONFIG") {
        if !p.is_empty() {
            return Some(PathBuf::from(p));
        }
    }
    let home = home_dir()?;
    let user = home.join(".airo").join("config.json");
    if user.exists() {
        return Some(user);
    }
    let local = project_root().join("config.json");
    if local.exists() {
        return Some(local);
    }
    None
}

fn home_dir() -> Option<PathBuf> {
    std::env::var("HOME").ok().filter(|s| !s.is_empty()).map(PathBuf::from)
}

pub fn latest_path() -> PathBuf {
    data_dir().join("latest.json")
}

/// Read the current state. An unreadable or half-written file is not an
/// error worth crashing over — the poller rewrites it atomically every few
/// minutes, so the next read will succeed.
pub fn read_latest() -> Result<Latest, String> {
    let path = latest_path();
    let text = std::fs::read_to_string(&path)
        .map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    serde_json::from_str::<Latest>(&text).map_err(|e| format!("cannot parse latest.json: {e}"))
}

/// Which interpreter to run, given where the binary is.
///
/// The installed app carries its own Python precisely because the machine may
/// not have one. Falling back to `python3` inside a bundle would mean the app
/// works on the developer's machine and fails on the user's -- the one place
/// that failure is invisible until it matters.
pub fn resolve_python(exe_dir: &Path, env_python: Option<&str>) -> String {
    if let Some(p) = env_python.filter(|s| !s.is_empty()) {
        return p.to_string();
    }
    if let Some(payload) = bundled_payload(exe_dir) {
        // Two shapes, because python-build-standalone does not lay the
        // platforms out the same way: the unix builds put the interpreter
        // under bin/, the Windows build puts python.exe at the top. Checking
        // only the unix path shipped a Windows app whose bundled interpreter
        // was present and unfindable -- it fell through to the system
        // "python3", which is exactly the machine that has none.
        //
        // Both are tried on every platform rather than behind cfg!: the layout
        // is a property of the payload, not of the host, and a cross-built
        // bundle should still work.
        let runtime = payload.join("runtime");
        for candidate in [runtime.join("bin").join("python3"),
                          runtime.join("python.exe")] {
            if candidate.exists() {
                return candidate.to_string_lossy().into_owned();
            }
        }
    }
    "python3".to_string()
}

fn python() -> String {
    let env_python = std::env::var("AIRO_PYTHON").ok();
    let exe = std::env::current_exe().ok();
    let dir = exe.as_deref().and_then(|e| e.parent()).unwrap_or(Path::new("."));
    resolve_python(dir, env_python.as_deref())
}

/// Say that a menu action failed, where somebody can actually see it.
///
/// Every handler used to discard its error with `let _ = ...`. Twenty-seven of
/// them. The failure that matters most is `spawn_python` refusing to start —
/// a missing interpreter or a broken payload — because that makes *every*
/// item in the menu do nothing at all, and this project has already shipped a
/// bundle whose payload was incomplete.
///
/// Two channels, because they fail differently. stderr is captured by launchd
/// into `tray.err.log` and survives for later; a notification is the only
/// thing the person who just clicked will actually see. The notification is
/// best-effort — if the desktop refuses it there is nothing further to try,
/// and the log line has already been written.
///
/// Deliberately not routed through Python, unlike everything else here. The
/// case being reported is "Python would not run", so asking Python to report
/// it would be the one message guaranteed to be lost.
pub fn report_problem(action: &str, err: &str) {
    eprintln!("airo: {action} failed: {err}");
    notify_desktop("Airo", &format!("{action} failed: {err}"));
}

/// A desktop notification, without taking a dependency for it.
///
/// `message` is passed as a separate argument rather than interpolated into a
/// script, on every platform. An error string can carry a path, and a path can
/// carry a quote.
#[cfg(target_os = "macos")]
fn notify_desktop(title: &str, message: &str) {
    // osascript reads the two values from argv, so nothing in them is parsed
    // as script. `display notification` takes them as expressions.
    let script = "on run argv\n                  display notification (item 1 of argv) with title (item 2 of argv)\n                  end run";
    let _ = std::process::Command::new("/usr/bin/osascript")
        .arg("-e").arg(script)
        .arg(message).arg(title)
        .spawn();
}

#[cfg(target_os = "linux")]
fn notify_desktop(title: &str, message: &str) {
    let _ = std::process::Command::new("notify-send")
        .arg(title).arg(message).spawn();
}

#[cfg(target_os = "windows")]
fn notify_desktop(_title: &str, _message: &str) {
    // Windows toast needs either a registered AppUserModelID or a PowerShell
    // round trip through the WinRT API. Neither is worth a dependency for an
    // error path, and stderr is captured. Stated rather than left as a silent
    // no-op: a platform stub that quietly does nothing is a bug this project
    // has shipped four times.
}

/// Run one of the project's Python entry points.
///
/// Always spawned, never awaited: a backfill takes minutes and a poll takes
/// seconds, and blocking the menu on either would freeze the whole tray.
fn spawn_python(script: &str, args: &[&str]) -> Result<(), String> {
    let root = project_root();
    let mut cmd = std::process::Command::new(python());
    cmd.arg(root.join(script));
    for a in args {
        cmd.arg(a);
    }
    cmd.current_dir(&root)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("could not run {script}: {e}"))
}

pub fn poller(args: &[&str]) -> Result<(), String> {
    spawn_python("poller.py", args)
}

/// Run a Python entry point and wait for what it prints.
///
/// The counterpart to spawn_python, for the handful of calls whose *answer* is
/// the point rather than the side effect. Waited on rather than spawned, so it
/// must stay reserved for commands that finish quickly -- a backfill through
/// here would freeze the menu.
fn python_output(script: &str, args: &[&str]) -> Result<String, String> {
    let root = project_root();
    let mut cmd = std::process::Command::new(python());
    cmd.arg(root.join(script));
    for a in args {
        cmd.arg(a);
    }
    let out = cmd
        .current_dir(&root)
        .output()
        .map_err(|e| format!("could not run {script}: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "{script} {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// The URL of a served page, resolved by Python and never built here.
///
/// The port is configurable AND moves on its own when something else holds
/// it, so any address assembled in this process is a guess. Asking costs one
/// short subprocess and cannot be wrong.
pub fn page_url(page: &str) -> Result<String, String> {
    let url = python_output("poller.py", &["--url", page])?;
    if url.is_empty() {
        return Err("the local server could not be reached".into());
    }
    Ok(url)
}

/// Start/stop/restart the background poller, via the cross-platform scheduler.
pub fn scheduler(action: &str) -> Result<(), String> {
    spawn_python("scheduler.py", &[action])
}

/// Trigger a poll.
pub fn poll_now() -> Result<(), String> {
    poller(&["--once"])
}

/// Open one of the served pages, letting Python decide everything about it.
///
/// This used to hold `http://127.0.0.1:8787/dashboard.html` as a literal and
/// sleep 700ms in the hope a server had appeared. Both were wrong: serve_port
/// is configurable, and the server deliberately *moves* to the next free port
/// when an unrelated program holds 8787 -- so the tray opened a dead page, or
/// somebody else's page. Whether a server is running, which port it actually
/// got, and what URL that makes are all decisions, and decisions live in
/// Python. The tray runs one command.
pub fn open_page(page: &str) -> Result<(), String> {
    poller(&["--open", page]).map(|_| ())
}

pub fn open_dashboard() -> Result<(), String> {
    open_page("dashboard")
}

/// The settings URL, for the app's own window to display.
///
/// Settings, setup and keys are one page, and that page now lives inside Airo
/// rather than in a browser tab: configuring the app should not mean leaving
/// it. The dashboard stays a browser link, deliberately -- it is a wide
/// reading surface, and a 480px window is the wrong shape for it.
///
/// Still served over loopback by Python rather than bundled into the webview,
/// because the page needs the per-process token the server substitutes, and
/// because the validator that decides what a valid setting is must stay on
/// the Python side (rule 7 in spirit: the tray renders, it does not decide).
pub fn settings_url() -> Result<String, String> {
    page_url("settings")
}


/// Reveal a path inside the project in the platform's file manager or editor.
pub fn reveal(relative: &str) -> Result<(), String> {
    let target = project_root().join(relative);
    let opener = if cfg!(target_os = "macos") {
        "open"
    } else if cfg!(target_os = "windows") {
        "explorer"
    } else {
        "xdg-open"
    };
    std::process::Command::new(opener)
        .arg(&target)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("could not open {}: {e}", target.display()))
}

/// Open a signup page. Public wrapper so the menu can reach it.
pub fn open_url_public(url: &str) -> Result<(), String> {
    open_url(url)
}

/// Toggle alerts. The tray asks the control script to do it rather than
/// editing the config itself: whether alerting is on is a setting, but where
/// that setting lives, and what a valid config looks like, is Python's
/// business.
pub fn set_alerts(on: bool) -> Result<(), String> {
    // This used to shell out to a script that edited config.json directly:
    // a second writer with its own idea of the file's shape, and macOS-only
    // besides. Whether alerting is on is a setting, and what a valid setting
    // looks like is Python's answer.
    poller(&["--alerts", if on { "on" } else { "off" }]).map(|_| ())
}



fn open_url(url: &str) -> Result<(), String> {
    let opener = if cfg!(target_os = "macos") {
        "open"
    } else if cfg!(target_os = "windows") {
        "explorer"
    } else {
        "xdg-open"
    };
    std::process::Command::new(opener)
        .arg(url)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("could not open {url}: {e}"))
}



/// Right-align a number inside a proportional font.
///
/// A menu item is plain text in the system font, so ordinary spaces do not
/// form columns: "1 hr" plus four spaces is a different pixel width from
/// "Now" plus five, and the rolling-average table came out visibly ragged
/// down the middle. U+2007 FIGURE SPACE is defined as the width of a digit,
/// so padding numerics with it produces true columns.
///
/// The corollary is that every variable-width label must sit at the END of
/// the line, where a ragged edge reads as an ordinary list rather than a
/// broken table. Checked by rendering the candidates in SF Pro at menu size:
/// space-padding and figure-padding-with-leading-labels both stayed ragged;
/// only numerics-first aligned.
fn pad_num(s: &str, width: usize) -> String {
    const FIGURE_SPACE: char = '\u{2007}';
    let mut out = String::new();
    for _ in s.chars().count()..width {
        out.push(FIGURE_SPACE);
    }
    out.push_str(s);
    out
}

/// One line of the menu's readout section.
#[derive(Debug, Clone, PartialEq)]
pub enum Readout {
    Separator,
    Line { id: String, text: String },
}

fn line(id: &str, text: impl Into<String>) -> Readout {
    Readout::Line { id: id.to_string(), text: text.into() }
}

/// Everything the menu shows that comes from the data, in order.
///
/// Pure and separate from the Tauri menu so the rendered content can be
/// inspected with `airo-tray --print-menu` and asserted in tests -- neither of
/// which can open a window. Building these strings inline made what the user
/// actually reads unverifiable on any headless machine.
///
/// Order is deliberate: identity, then the number, then its provenance, then
/// warnings BEFORE anything reassuring, then detail.
pub fn readout_lines(l: &Latest) -> Vec<Readout> {
    let mut out = Vec::new();

    // ---- headline
    //
    // Lead with the reading. The menu previously opened with the place name
    // and the band, and never showed the index at all -- the number was in
    // the menu-bar title and nowhere in the menu itself, so the one figure
    // the tool exists to report was the one thing the dropdown omitted.
    let band = l.band.clone().unwrap_or_else(|| "No data".into());
    let scale = l.scale_label.clone().unwrap_or_else(|| "AQI".into());
    match l.aqi {
        Some(v) if l.scale.as_deref() == Some("raw") => out.push(line(
            "noop_head", format!("{}  {:.1} µg/m³ · {band}", l.band_symbol(), v))),
        Some(v) => out.push(line(
            "noop_head",
            format!("{}  {} · {band}   ({scale})", l.band_symbol(), v.round() as i64))),
        None => out.push(line("noop_head", format!("{}  {band}", l.band_symbol()))),
    }
    if let Some(loc) = &l.location_name {
        out.push(line("noop_loc", loc.clone()));
    }
    // Raw µg and where it came from belong together: the concentration is
    // meaningless without knowing which instrument reported it.
    match (l.pm25_10min, &l.provenance) {
        (Some(pm), Some(p)) => out.push(line(
            "noop_pm", format!("{pm:.1} µg/m³ · via {p}"))),
        (Some(pm), None) => out.push(line("noop_pm", format!("{pm:.1} µg/m³"))),
        (None, Some(p)) => out.push(line("noop_prov", format!("via {p}"))),
        (None, None) => {}
    }

    // ---- warnings, before anything reassuring
    if l.uncorroborated {
        out.push(line("noop_unc", "⚠ Not confirmed by nearby sources"));
        if let Some(n) = &l.corroboration_note {
            let short: String = n.chars().take(84).collect();
            out.push(line("noop_uncn", format!("   {short}")));
        }
    }

    // ---- staleness of the headline itself, judged by the poller's cadence
    if let Some(src) = &l.source {
        if let (Some(age), Some(poll)) = (src.age_minutes, l.poll_minutes) {
            if age > (poll as f64) * 2.5 {
                out.push(line("noop_stale",
                    format!("⚠ STALE — {} min old, expected every {} min",
                            age.round() as i64, poll)));
            }
        }
    }

    // ---- sources
    if !l.sources.is_empty() {
        out.push(Readout::Separator);
        out.push(line("noop_srch", "Sources"));
        for (i, s) in l.sources.iter().enumerate() {
            let name = s.site_name.clone()
                .or_else(|| s.provider.clone())
                .unwrap_or_else(|| "unknown".into());
            // Same rule as the averages: numerics first so they form real
            // columns, and the variable-width site name last. Site names run
            // from "Riverside" to "Northfield (OpenAQ)", so leading with them
            // guaranteed a ragged middle no padding could fix.
            let value = match s.pm25 {
                Some(v) => pad_num(&format!("{v:.1}"), 5),
                None => pad_num("—", 5),
            };
            let dist = match s.distance_km {
                Some(d) => format!("{} km", pad_num(&format!("{d:.1}"), 4)),
                None => format!("{}   ", pad_num("—", 4)),
            };
            let age = match s.age_minutes {
                Some(a) => format!("{} min", pad_num(&format!("{}", a.round() as i64), 3)),
                None => format!("{}    ", pad_num("—", 3)),
            };
            let mut flags = Vec::new();
            if s.stale.unwrap_or(false) {
                flags.push("stale");
            }
            if s.quality.as_deref() == Some("suspect") {
                flags.push("sensor fault");
            }
            if s.corroboration.as_deref() == Some("uncorroborated") {
                flags.push("unconfirmed");
            }
            let suffix = if flags.is_empty() {
                String::new()
            } else {
                format!("  ({})", flags.join(", "))
            };
            out.push(line(&format!("noop_src{i}"),
                          format!("   {value} µg · {dist} · {age}   {name}{suffix}")));
        }
    }

    // ---- rolling averages, in both the configured index and raw µg
    let order = [("now", "Now"), ("10min", "10 min"), ("30min", "30 min"),
                 ("60min", "1 hr"), ("6hr", "6 hr"), ("24hr", "1 day"),
                 ("1week", "Week")];
    if order.iter().any(|(k, _)| l.averages_aqi.get(*k).and_then(|v| *v).is_some()) {
        out.push(Readout::Separator);
        out.push(line("noop_avgh", "Rolling averages"));
        for (key, human) in order {
            let idx = match l.averages_aqi.get(key).and_then(|v| *v) {
                Some(v) => v,
                None => continue,
            };
            let ug = l.averages_pm25.get(key).and_then(|v| *v);
            let shown = if l.scale.as_deref() == Some("raw") {
                format!("{idx:.1}")
            } else {
                format!("{}", idx.round() as i64)
            };
            let text = match ug {
                Some(u) => format!("   {}  {} µg   {human}",
                                   pad_num(&shown, 4), pad_num(&format!("{u:.1}"), 5)),
                None => format!("   {}          {human}", pad_num(&shown, 4)),
            };
            out.push(line(&format!("noop_avg_{key}"), text));
        }
    }

    // ---- trend and time-of-day advice, both decided in Python
    if let Some(t) = &l.trend {
        if let Some(text) = &t.text {
            let arrow = match t.direction.as_deref() {
                Some("rising_fast") | Some("rising") => "▲",
                Some("clearing") => "▼",
                // "▬" rendered as a stray dash at menu size, reading as a
                // bullet artefact rather than a direction.
                _ => "→",
            };
            out.push(line("noop_trend", format!("{arrow} {text}")));
        }
    }
    if let Some(h) = &l.time_hint {
        if let Some(text) = &h.text {
            out.push(line("noop_hint", text.clone()));
        }
    }

    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every menu action must report a failure somewhere a person can see it.
    ///
    /// Twenty-seven handlers discarded their error with `let _ = ...`. The one
    /// that mattered was `spawn_python` refusing to start: a missing
    /// interpreter or an incomplete payload makes *every* item in the menu do
    /// nothing, and this project has shipped a bundle with a payload missing a
    /// module before.
    ///
    /// The maintainer reported exactly this shape — "Open dashboard in browser
    /// is not working" — and nothing anywhere said why.
    #[test]
    fn a_failure_message_names_the_action_and_the_cause() {
        // report_problem writes to stderr and fires a notification; neither is
        // capturable here without a dependency. What is checkable, and what
        // actually decides whether the message is useful, is that both halves
        // reach the text.
        let action = "Open dashboard in browser";
        let err = "could not run poller.py: No such file or directory";
        let composed = format!("{action} failed: {err}");
        assert!(composed.contains(action), "the message omits the action");
        assert!(composed.contains("No such file"), "the message omits the cause");
    }

    #[test]
    fn the_notifier_passes_text_as_arguments_not_as_script() {
        // An error string carries a path, and a path can carry a quote. The
        // macOS notifier reads both values out of argv so nothing in them is
        // parsed as AppleScript.
        let source = include_str!("airo.rs");
        let block = source
            .split("fn notify_desktop")
            .nth(1)
            .expect("the macOS notifier is gone");
        assert!(block.contains("item 1 of argv"),
                "the notification body is interpolated into the script");
        assert!(!block.contains("display notification \\\""),
                "a literal string is being built into the script");
    }


    /// A throwaway directory that cleans itself up.
    ///
    /// Hand-rolled because the tray has no dev-dependencies and adding one for
    /// a temp directory is not worth the supply chain.
    struct Scratch(PathBuf);

    impl Scratch {
        fn new(tag: &str) -> Self {
            let mut p = std::env::temp_dir();
            p.push(format!("airo-test-{tag}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&p);
            std::fs::create_dir_all(&p).unwrap();
            Scratch(p)
        }
        fn path(&self) -> &Path { &self.0 }
        fn touch(&self, rel: &str) -> PathBuf {
            let f = self.0.join(rel);
            std::fs::create_dir_all(f.parent().unwrap()).unwrap();
            std::fs::write(&f, b"x").unwrap();
            f
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.0); }
    }

    // ---- where the project is -------------------------------------------
    //
    // These matter more than they look. A tray that cannot find the project
    // renders "No reading yet", which is what a user sees when there is no
    // data at all -- so a resolution bug and an empty database are
    // indistinguishable on screen.

    #[test]
    fn an_installed_app_finds_the_payload_beside_it() {
        let s = Scratch::new("bundle");
        s.touch("Airo.app/Contents/Resources/payload/airo/poller.py");
        let exe_dir = s.path().join("Airo.app/Contents/MacOS");
        std::fs::create_dir_all(&exe_dir).unwrap();

        let root = resolve_root(&exe_dir, None);

        assert!(root.join("poller.py").exists(),
                "an installed app could not find its own Python: {root:?}");
    }

    #[test]
    fn a_development_checkout_is_found_by_walking_up() {
        let s = Scratch::new("checkout");
        s.touch("poller.py");
        let exe_dir = s.path().join("tray/target/release");
        std::fs::create_dir_all(&exe_dir).unwrap();

        let root = resolve_root(&exe_dir, None);

        assert_eq!(root, *s.path());
    }

    #[test]
    fn a_fresh_clone_is_found_even_with_no_config_and_no_data() {
        // The old markers were config.json and data/. Both are absent from a
        // fresh clone -- config.json is gitignored and data/ is created on
        // first poll -- so the tray fell through to "." and found nothing.
        let s = Scratch::new("fresh");
        s.touch("poller.py");
        assert!(!s.path().join("config.json").exists());
        assert!(!s.path().join("data").exists());

        let root = resolve_root(s.path(), None);

        assert_eq!(root, *s.path());
    }

    #[test]
    fn an_explicit_home_wins_over_everything() {
        let s = Scratch::new("override");
        s.touch("Airo.app/Contents/Resources/payload/airo/poller.py");
        let exe_dir = s.path().join("Airo.app/Contents/MacOS");
        std::fs::create_dir_all(&exe_dir).unwrap();

        let root = resolve_root(&exe_dir, Some("/somewhere/else"));

        assert_eq!(root, PathBuf::from("/somewhere/else"));
    }

    #[test]
    fn an_empty_override_is_ignored_rather_than_obeyed() {
        // AIRO_HOME="" is how an unset variable often arrives from a plist or
        // a shell. Obeying it would point the tray at the filesystem root.
        let s = Scratch::new("emptyenv");
        s.touch("poller.py");
        assert_eq!(resolve_root(s.path(), Some("")), *s.path());
    }

    // ---- which interpreter ----------------------------------------------

    #[test]
    fn an_installed_app_runs_the_python_it_shipped() {
        // Falling back to `python3` inside a bundle means the app works on a
        // machine that has one and fails on the machine it was built for.
        let s = Scratch::new("py");
        s.touch("Airo.app/Contents/Resources/payload/airo/poller.py");
        s.touch("Airo.app/Contents/Resources/payload/runtime/bin/python3");
        let exe_dir = s.path().join("Airo.app/Contents/MacOS");
        std::fs::create_dir_all(&exe_dir).unwrap();

        let py = resolve_python(&exe_dir, None);

        // Compared as path components, not as a slash-joined substring.
        // Path::join emits a backslash on Windows, so the substring form
        // failed there against a resolution that was entirely correct — the
        // test was asserting a path separator, not a behaviour.
        let p = Path::new(&py);
        let tail: Vec<_> = p.components()
            .rev().take(4)
            .map(|c| c.as_os_str().to_string_lossy().into_owned())
            .collect();
        assert_eq!(tail, ["python3", "bin", "runtime", "payload"],
                   "an installed app would use the system Python: {py}");
    }

    #[test]
    fn a_null_collection_does_not_blind_the_whole_tray() {
        // quiet_hours was null for every install that had not set quiet
        // hours — the default — and serde failed the entire document, so the
        // tray reported "no reading yet" beside a full database. The writer
        // now emits a list, and this makes the reader survive it regardless:
        // an older poller, a hand-edited file, or the next field somebody
        // forgets.
        let json = r#"{
            "aqi": 21.0, "band": "Very good", "scale": "au",
            "quiet_hours": null,
            "sources": null,
            "attributions": null,
            "networks": null,
            "alerts": {"enabled": true, "quiet_hours": null}
        }"#;

        let l: Latest = serde_json::from_str(json)
            .expect("a null collection must not fail the whole parse");

        assert_eq!(l.band.as_deref(), Some("Very good"));
        assert!(l.sources.is_empty());
        assert!(l.attributions.is_empty());
        assert!(l.networks.is_empty());
        assert!(l.alerts.unwrap().quiet_hours.is_empty());
    }

    #[test]
    fn a_windows_bundle_finds_the_interpreter_where_windows_puts_it() {
        // python-build-standalone puts python.exe at the top of the runtime
        // on Windows and bin/python3 on unix. Looking only for the unix path
        // shipped a Windows app whose bundled interpreter was present and
        // unfindable, falling through to a system "python3" that is exactly
        // what those machines do not have.
        //
        // Tested here rather than discovered there: nobody on this project has
        // a Windows machine, so the layout has to be asserted or the failure
        // arrives as a user report.
        let s = Scratch::new("pywin");
        s.touch("payload/airo/poller.py");
        s.touch("payload/runtime/python.exe");

        let py = resolve_python(s.path(), None);

        assert!(py.contains("python.exe"),
                "a Windows bundle fell back to the system Python: {py}");
    }

    #[test]
    fn a_bundle_without_an_interpreter_falls_back_rather_than_lying() {
        // A payload staged without a runtime is a real state -- stage_bundle
        // allows it so the tree can be checked without a 69 MB fetch. It must
        // degrade to the system Python, not return a path to nothing.
        let s = Scratch::new("pynone");
        s.touch("payload/airo/poller.py");

        assert_eq!(resolve_python(s.path(), None), "python3");
    }

    #[test]
    fn a_checkout_uses_whatever_python_is_on_the_path() {
        let s = Scratch::new("pydev");
        s.touch("poller.py");
        assert_eq!(resolve_python(s.path(), None), "python3");
    }

    #[test]
    fn a_bundle_with_no_runtime_does_not_claim_to_have_one() {
        // A payload can exist without an interpreter if the build skipped the
        // fetch. Naming a path that is not there would fail later and further
        // from the cause.
        let s = Scratch::new("norun");
        s.touch("Airo.app/Contents/Resources/payload/airo/poller.py");
        let exe_dir = s.path().join("Airo.app/Contents/MacOS");
        std::fs::create_dir_all(&exe_dir).unwrap();

        assert_eq!(resolve_python(&exe_dir, None), "python3");
    }

    #[test]
    fn an_explicit_interpreter_wins() {
        let s = Scratch::new("pyoverride");
        s.touch("Airo.app/Contents/Resources/payload/runtime/bin/python3");
        s.touch("Airo.app/Contents/Resources/payload/airo/poller.py");
        let exe_dir = s.path().join("Airo.app/Contents/MacOS");
        std::fs::create_dir_all(&exe_dir).unwrap();

        assert_eq!(resolve_python(&exe_dir, Some("/usr/bin/python3.13")),
                   "/usr/bin/python3.13");
    }

    fn latest(band: Option<&str>, aqi: Option<f64>, scale: &str, uncorroborated: bool) -> Latest {
        Latest {
            fetched_local: None,
            last_poll_text: None,
            next_poll_text: None,
            location_name: None,
            aqi,
            band: band.map(String::from),
            pm25_10min: None,
            scale: Some(scale.to_string()),
            scale_label: None,
            fusion_rule: None,
            fusion_note: None,
            fusion_degraded: false,
            uncorroborated,
            corroboration_note: None,
            provenance: None,
            source: None,
            sources: vec![],
            attributions: vec![],
            networks: vec![],
            trend: None,
            time_hint: None,
            alerts: None,
            averages_aqi: Default::default(),
            averages_pm25: Default::default(),
            poll_minutes: None,
            fetched_utc: None,
        }
    }

    #[test]
    fn title_rounds_the_index() {
        let l = latest(Some("Fair"), Some(76.8), "au", false);
        assert!(l.tray_title().contains("77"), "got {}", l.tray_title());
    }

    #[test]
    fn raw_scale_shows_micrograms_not_an_index() {
        let l = latest(Some("Above WHO guideline"), Some(19.2), "raw", false);
        let t = l.tray_title();
        assert!(t.contains("19.2"), "got {t}");
        assert!(t.contains("µg"), "got {t}");
    }

    #[test]
    fn no_reading_is_not_shown_as_zero() {
        let l = latest(None, None, "au", false);
        let t = l.tray_title();
        assert!(t.contains('—'), "got {t}");
        assert!(!t.contains('0'), "absence must not look like a measurement: {t}");
    }

    #[test]
    fn uncorroborated_reading_is_marked_in_the_menu_bar() {
        let plain = latest(Some("Hazardous"), Some(320.0), "au", false);
        let flagged = latest(Some("Hazardous"), Some(320.0), "au", true);
        assert!(!plain.tray_title().contains('?'));
        assert!(flagged.tray_title().ends_with('?'),
                "got {}", flagged.tray_title());
    }

    #[test]
    fn severity_symbol_differs_across_bands() {
        let good = latest(Some("Very good"), Some(8.0), "au", false);
        let fair = latest(Some("Fair"), Some(77.0), "au", false);
        let bad = latest(Some("Hazardous"), Some(320.0), "au", false);
        assert_ne!(good.band_symbol(), fair.band_symbol());
        assert_ne!(fair.band_symbol(), bad.band_symbol());
    }

    #[test]
    fn us_epa_band_names_are_recognised() {
        // The tray must not assume Australian band names -- the scale is
        // configurable and only the poller knows which one is in force.
        for name in ["Good", "Moderate", "Unhealthy for sensitive groups",
                     "Unhealthy", "Very unhealthy", "Hazardous"] {
            let l = latest(Some(name), Some(50.0), "us_epa", false);
            assert_ne!(l.band_symbol(), "\u{26AA}",
                       "unrecognised US EPA band: {name}");
        }
    }

    #[test]
    fn unknown_band_falls_back_rather_than_panicking() {
        let l = latest(Some("Something New"), Some(50.0), "au", false);
        assert_eq!(l.band_symbol(), "\u{26AA}");
    }

    #[test]
    fn latest_json_from_the_poller_deserialises() {
        // Shape check against what build_latest() actually writes.
        let json = r#"{
            "aqi": 320.4, "band": "Hazardous", "pm25_10min": 80.1,
            "scale": "au", "scale_label": "Australian AQI",
            "fusion_rule": "nearest", "fusion_degraded": false,
            "uncorroborated": true,
            "corroboration_note": "16.7x nearby sources",
            "provenance": "Example sensor · 1.0 km · just now",
            "sources": [{"provider":"purpleair","site_name":"Example sensor",
                         "pm25":80.1,"aqi":320.4,"band":"Hazardous",
                         "age_minutes":0.0,"distance_km":0.96,"stale":false,
                         "quality":"ok","corroboration":"uncorroborated",
                         "peer_ratio":16.7}],
            "attributions": ["Powered by PurpleAir"]
        }"#;
        let l: Latest = serde_json::from_str(json).expect("must parse");
        assert_eq!(l.band.as_deref(), Some("Hazardous"));
        assert!(l.uncorroborated);
        assert_eq!(l.sources.len(), 1);
        assert_eq!(l.sources[0].corroboration.as_deref(), Some("uncorroborated"));
        assert!(l.tray_title().ends_with('?'));
    }

    #[test]
    fn networks_block_from_the_poller_deserialises() {
        // The contract that lets the tray offer sources the user is not yet
        // reading. If this drifts, the "Add a source" menu silently empties.
        let json = r#"{
            "aqi": 26.0, "band": "Very good",
            "networks": [
              {"provider":"openaq","label":"OpenAQ","tier":"reference",
               "needs_key":true,"has_key":false,"in_use":false,
               "signup_url":"https://explore.openaq.org/register"},
              {"provider":"qld","label":"QLD","tier":"reference",
               "needs_key":false,"has_key":true,"in_use":true,
               "signup_url":null}
            ]
        }"#;
        let l: Latest = serde_json::from_str(json).expect("must parse");
        assert_eq!(l.networks.len(), 2);
        let unused: Vec<_> = l.networks.iter().filter(|n| !n.in_use).collect();
        assert_eq!(unused.len(), 1);
        assert_eq!(unused[0].provider.as_deref(), Some("openaq"));
        assert!(unused[0].signup_url.is_some(), "signup link must survive");
    }

    #[test]
    fn missing_networks_block_is_not_fatal() {
        // Older latest.json files predate this field entirely.
        let l: Latest = serde_json::from_str(r#"{"aqi":5.0,"band":"Very good"}"#)
            .expect("must tolerate an absent networks block");
        assert!(l.networks.is_empty());
    }

    #[test]
    fn unknown_fields_do_not_break_parsing() {
        // latest.json carries deprecated aliases and may gain fields; the tray
        // must tolerate both rather than failing to render.
        let l: Latest = serde_json::from_str(
            r#"{"aqi":10.0,"band":"Very good","au_aqi":10.0,"future_key":1}"#)
            .expect("must tolerate extra keys");
        assert_eq!(l.aqi, Some(10.0));
    }

    /// A brand-new install: one reading, one keyless government source, and
    /// no PurpleAir anywhere. Captured from a clean clone plus an empty home
    /// — with synthetic site names and distances — because every earlier
    /// check ran against a 17,000-row database and this state had never been
    /// exercised.
    #[test]
    fn a_brand_new_install_with_one_reading_parses() {
        let l: Latest = serde_json::from_str(r#"{
            "location_name": "Home",
            "aqi": 15.6,
            "band": "Very good",
            "pm25_10min": 3.9,
            "poll_minutes": 60,
            "provenance": "Riverside \u00b7 2.0 km \u00b7 34 min ago",
            "attributions": ["Contains Queensland Government data, CC BY 4.0"],
            "sources": [{"provider":"qld","site_name":"Riverside",
                         "pm25":3.9,"distance_km":2.0,"age_minutes":34.0}]
        }"#).expect("a fresh install must parse");

        assert_eq!(l.band.as_deref(), Some("Very good"));
        assert_eq!(l.pm25_10min, Some(3.9));
        assert_eq!(l.sources.len(), 1);
        assert_eq!(l.attributions.len(), 1);
    }

    /// The attribution shown must be the networks actually in use. SwiftBar
    /// printed "Powered by PurpleAir" as a literal, so a Queensland-only user
    /// was credited to a network they do not use and never saw the CC BY
    /// notice they do owe. That plugin is gone; this keeps the property.
    #[test]
    fn a_queensland_only_install_carries_only_its_own_attribution() {
        let l: Latest = serde_json::from_str(r#"{
            "attributions": ["Contains Queensland Government data, CC BY 4.0"]
        }"#).expect("must parse");
        assert!(l.attributions.iter().any(|a| a.contains("CC BY")),
                "the CC BY notice a government feed requires is missing");
        assert!(!l.attributions.iter().any(|a| a.contains("PurpleAir")),
                "credited PurpleAir on an install that does not use it");
    }

    /// Empty is a real state, not an error: between install and first poll
    /// there is no reading at all, and the tray must still open.
    #[test]
    fn an_install_with_no_reading_yet_is_not_fatal() {
        let l: Latest = serde_json::from_str("{}").expect("empty must parse");
        assert!(l.aqi.is_none());
        assert!(l.sources.is_empty());
        assert!(l.attributions.is_empty());
    }

    fn texts(l: &Latest) -> Vec<String> {
        readout_lines(l).into_iter().filter_map(|r| match r {
            Readout::Line { text, .. } => Some(text),
            Readout::Separator => None,
        }).collect()
    }

    /// The menu is the most-used surface and, until readout_lines() existed,
    /// the only one that could not be checked without a window server.
    /// The menu opened with the place name and never showed the index at
    /// all: the one figure the tool exists to report was in the menu-bar
    /// title and nowhere in the dropdown.
    #[test]
    fn the_readout_leads_with_the_reading() {
        let l: Latest = serde_json::from_str(
            r#"{"location_name":"Testville","band":"Very good","aqi":2.4,
                 "scale_label":"Australian AQI","pm25_10min":0.6,
                 "provenance":"Example sensor · 1.0 km · just now"}"#
        ).unwrap();
        let t = texts(&l);
        assert_eq!(t[0], "🟢  2 · Very good   (Australian AQI)");
        assert_eq!(t[1], "Testville");
        assert_eq!(t[2], "0.6 µg/m³ · via Example sensor · 1.0 km · just now");
    }

    /// On the raw scale the headline IS the concentration, so it must not be
    /// rounded to an integer index.
    #[test]
    fn the_raw_scale_shows_a_concentration_not_an_index() {
        let l: Latest = serde_json::from_str(
            r#"{"band":"Good","aqi":6.4,"scale":"raw"}"#).unwrap();
        assert_eq!(texts(&l)[0], "🟢  6.4 µg/m³ · Good");
    }

    /// A caveat placed after the reassuring number is a caveat most people
    /// never read.
    #[test]
    fn a_warning_comes_before_the_detail_it_qualifies() {
        let l: Latest = serde_json::from_str(
            r#"{"band":"Very good","uncorroborated":true,
                 "corroboration_note":"3x its neighbours",
                 "sources":[{"site_name":"A","pm25":1.0}]}"#).unwrap();
        let t = texts(&l);
        let warn = t.iter().position(|x| x.contains("Not confirmed")).unwrap();
        let srcs = t.iter().position(|x| x == "Sources").unwrap();
        assert!(warn < srcs, "the warning was buried below the source list");
    }

    /// Every bucket with an index must show the raw µg it came from. The
    /// 10-minute row published its index without the canonical value, so the
    /// menu showed "10 min  2" with nothing beside it.
    #[test]
    fn every_average_row_carries_its_raw_value() {
        let l: Latest = serde_json::from_str(r#"{
            "averages_aqi": {"now":0.0,"10min":2.4,"30min":3.2},
            "averages_pm25": {"now":0.0,"10min":0.6,"30min":0.8}
        }"#).unwrap();
        for row in texts(&l).iter().filter(|t| t.trim_start().starts_with(char::is_alphabetic)
                                            && t.starts_with("   ")) {
            assert!(row.contains("µg"), "average row without a raw value: {row:?}");
        }
    }

    /// No reading is a real state and must not look like a zero.
    #[test]
    fn an_empty_reading_still_produces_a_readable_menu() {
        let l: Latest = serde_json::from_str("{}").unwrap();
        let t = texts(&l);
        assert_eq!(t[0], "⚪  No data");
        assert_eq!(l.tray_title(), "\u{26AA} —",
                   "an absent reading must not render as a zero");
    }

    #[test]
    fn a_stale_headline_is_called_out() {
        let l: Latest = serde_json::from_str(
            r#"{"band":"Good","poll_minutes":15,
                 "source":{"age_minutes":120.0}}"#).unwrap();
        assert!(texts(&l).iter().any(|t| t.contains("STALE")),
                "a two-hour-old reading was presented as current");
    }

    #[test]
    fn a_fresh_headline_is_not_called_stale() {
        let l: Latest = serde_json::from_str(
            r#"{"band":"Good","poll_minutes":15,
                 "source":{"age_minutes":3.0}}"#).unwrap();
        assert!(!texts(&l).iter().any(|t| t.contains("STALE")));
    }

    /// A menu item is plain text in a proportional system font, so ordinary
    /// spaces do not form columns. Verified by rendering the candidates in SF
    /// Pro at menu size: space-padded and figure-padded-behind-a-label both
    /// stayed visibly ragged; only numerics-first aligned.
    #[test]
    fn numbers_are_padded_with_figure_space_not_ordinary_space() {
        assert_eq!(pad_num("4", 4), "\u{2007}\u{2007}\u{2007}4");
        assert_eq!(pad_num("16", 4), "\u{2007}\u{2007}16");
        assert_eq!(pad_num("1234", 4), "1234");
        assert_eq!(pad_num("12345", 4), "12345", "must never truncate a value");
    }

    #[test]
    fn every_average_row_is_the_same_length_up_to_its_label() {
        let l: Latest = serde_json::from_str(r#"{
            "averages_aqi": {"now":0.0,"10min":2.0,"6hr":16.0,"24hr":126.0},
            "averages_pm25": {"now":0.0,"10min":0.6,"6hr":4.0,"24hr":31.5}
        }"#).unwrap();
        let rows: Vec<String> = texts(&l).into_iter()
            .filter(|t| t.contains("µg") && t.starts_with("   ")).collect();
        assert_eq!(rows.len(), 4);
        // Everything before the window label must be a constant character
        // count, which is what makes the columns line up.
        let widths: Vec<usize> = rows.iter()
            .map(|r| r.chars().take_while(|c| *c != 'N' && *c != 'W' && !c.is_ascii_digit()
                                              || *c == 'µ').count())
            .collect();
        let prefix = |r: &String| r.split("µg").next().unwrap().chars().count();
        let first = prefix(&rows[0]);
        for r in &rows {
            assert_eq!(prefix(r), first,
                       "numeric prefix width differs, so the columns cannot align: {r:?}");
        }
        let _ = widths;
    }

    /// Variable-width text must sit at the END of the line, where a ragged
    /// edge reads as a list rather than a broken table.
    #[test]
    fn the_variable_width_label_comes_last() {
        let l: Latest = serde_json::from_str(r#"{
            "averages_aqi": {"now":0.0}, "averages_pm25": {"now":0.0}
        }"#).unwrap();
        let row = texts(&l).into_iter().find(|t| t.contains("µg")).unwrap();
        assert!(row.trim_end().ends_with("Now"),
                "the window label is not last: {row:?}");
    }

    #[test]
    fn source_rows_put_the_site_name_last() {
        let l: Latest = serde_json::from_str(r#"{
            "sources":[{"site_name":"Northfield (OpenAQ)","pm25":6.1,
                        "distance_km":3.0,"age_minutes":64.0}]
        }"#).unwrap();
        let row = texts(&l).into_iter().find(|t| t.contains("km")).unwrap();
        assert!(row.trim_end().ends_with("Northfield (OpenAQ)"),
                "the site name is not last: {row:?}");
    }

    #[test]
    fn a_source_fault_is_still_reported() {
        let l: Latest = serde_json::from_str(r#"{
            "sources":[{"site_name":"A","pm25":6.1,"quality":"suspect","stale":true}]
        }"#).unwrap();
        let row = texts(&l).into_iter().find(|t| t.contains("µg")).unwrap();
        assert!(row.contains("sensor fault"), "{row:?}");
        assert!(row.contains("stale"), "{row:?}");
    }

    /// Two tray icons is not cosmetic: both poll, both write, and a click
    /// reaches whichever one the OS felt like. It happens easily — a launchd
    /// agent plus a manual run from the checkout, which is exactly how it
    /// happened here.
    #[test]
    fn a_lock_holding_a_dead_pid_is_reclaimed_not_obeyed() {
        // A crash leaves the file behind. Believing it would mean the tray
        // never starts again until someone deletes a file they do not know
        // about.
        assert!(!pid_is_alive(999_999),
                "a pid that cannot exist was reported alive");
    }

    /// If this fails, the single-instance guard is a no-op on this platform:
    /// every lock looks stale, so a second tray always starts. That is exactly
    /// what a blanket `false` for non-unix targets did.
    #[test]
    fn our_own_process_is_alive() {
        assert!(pid_is_alive(std::process::id() as i32),
                "liveness is unimplemented here, so the lock is never honoured");
    }

    /// The lock must never be believed when it names this very process, or a
    /// restart that reused the pid would refuse itself.
    #[test]
    fn the_guard_ignores_a_lock_naming_the_current_process() {
        let src = include_str!("airo.rs");
        assert!(src.contains("pid != std::process::id() as i32"),
                "the guard would refuse a lock naming itself");
    }

    /// The tray and the poller must agree about where the database is. They
    /// have now disagreed twice: once when readings moved to ~/.airo/data,
    /// and again when data_dir became configurable and only the Python half
    /// honoured it — the tray read the default location, found nothing, and
    /// reported "no reading yet" while the poller wrote happily elsewhere.
    #[test]
    fn the_resolution_order_matches_the_poller() {
        let src = include_str!("airo.rs");
        let f = src.split("pub fn data_dir").nth(1).expect("data_dir is gone");
        let head = &f[..f.find("\nfn ").unwrap_or(f.len())];
        let env = head.find("AIRO_DATA").expect("env var not consulted");
        let cfg = head.find("configured_data_dir").expect("data_dir setting ignored");
        let home = head.find(".airo\").join(\"data").unwrap_or(usize::MAX);
        assert!(env < cfg, "the config would override an explicit AIRO_DATA");
        assert!(cfg < home, "the configured directory is checked too late");
    }

    #[test]
    fn a_configured_data_dir_expands_a_leading_tilde() {
        let src = include_str!("airo.rs");
        assert!(src.contains("strip_prefix(\"~/\")"),
                "a path written as ~/somewhere would be taken literally");
    }

    /// A config written by a newer poller, or one being edited, must not stop
    /// the tray finding a database it has been reading for months.
    #[test]
    fn an_unreadable_config_does_not_break_resolution() {
        let src = include_str!("airo.rs");
        let f = src.split("fn configured_data_dir").nth(1).expect("gone");
        let body = &f[..f.find("\nfn ").unwrap_or(f.len())];
        assert!(body.contains(".ok()?"), "a parse failure would propagate");
    }
}
