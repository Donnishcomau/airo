// SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
// SPDX-License-Identifier: AGPL-3.0-or-later

//! Airo tray — a menu-bar / system-tray readout for macOS, Windows and Linux.
//!
//! Replaces the macOS-only SwiftBar and Ubersicht widgets with one binary that behaves the
//! same on all three platforms, and carries the same actions.
//!
//! Design rule, repeated because it is the one that matters: this process
//! contains no air-quality logic. It reads `~/.airo/data/latest.json`, which the
//! Python poller writes after fusing every configured source, and renders it.
//! Any threshold, band boundary or fusion rule added here would be a second
//! implementation of a health-relevant decision, free to drift out of step
//! with the dashboard. Put it in Python instead.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod airo;

use std::time::Duration;
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::TrayIconBuilder,
    Emitter, Manager, WebviewUrl, WebviewWindowBuilder,
};

/// How often to re-read latest.json. This is a cheap local file read, not a
/// network call, so it can be frequent — the point is that the tray reflects
/// a manual poll almost immediately.
const REFRESH_SECS: u64 = 20;

#[tauri::command]
fn get_latest() -> Result<airo::Latest, String> {
    airo::read_latest()
}

#[tauri::command]
fn poll_now() -> Result<(), String> {
    airo::poll_now()
}

/// Build the whole menu from the current reading.
///
/// Rebuilt on every refresh rather than mutated, because the top section
/// *is* the data — the reading, its provenance and each source. A static
/// menu would mean opening it to find out nothing had changed.
fn build_menu(app: &tauri::AppHandle, latest: Option<&airo::Latest>) -> tauri::Result<Menu<tauri::Wry>> {
    let menu = Menu::new(app)?;

    // A disabled item is how a tray menu shows data. Everything above the
    // first separator is a readout, not a control.
    macro_rules! label {
        ($id:expr, $text:expr) => {
            menu.append(&MenuItem::with_id(app, $id, $text, false, None::<&str>)?)?
        };
    }

    let l = match latest {
        Some(l) => l,
        None => {
            label!("noop_nodata", "No reading yet");
            menu.append(&PredefinedMenuItem::separator(app)?)?;
            menu.append(&MenuItem::with_id(app, "setup", "Run setup…", true, None::<&str>)?)?;
            menu.append(&MenuItem::with_id(app, "poll", "Poll now", true, None::<&str>)?)?;
            menu.append(&MenuItem::with_id(app, "quit", "Quit Airo", true, None::<&str>)?)?;
            return Ok(menu);
        }
    };

    // Every data-derived line comes from one pure function, so what the menu
    // actually says can be inspected (`airo-tray --print-menu`) and asserted
    // in tests without a window server. Building the strings inline made the
    // rendered content unverifiable on a headless CI machine -- and on this
    // one, where screen recording is not permitted.
    for line in airo::readout_lines(l) {
        match line {
            airo::Readout::Separator => {
                menu.append(&PredefinedMenuItem::separator(app)?)?;
            }
            airo::Readout::Line { id, text } => label!(&id, &text),
        }
    }

    // ================= controls =================
    menu.append(&PredefinedMenuItem::separator(app)?)?;
    // Four controls, two of which were indistinguishable pairs: "Poll now"
    // fetches from the network while "Refresh" only re-read latest.json, and
    // "Open dashboard" opens a browser while "Show detail window" opens a
    // Tauri window on the same data. Named by what they do, and the cheap
    // one of each pair now says so.
    menu.append(&MenuItem::with_id(app, "poll", "Fetch a new reading", true, None::<&str>)?)?;
    menu.append(&MenuItem::with_id(app, "refresh", "Reload this menu", true, None::<&str>)?)?;
    menu.append(&PredefinedMenuItem::separator(app)?)?;
    menu.append(&MenuItem::with_id(app, "dashboard", "Open dashboard in browser", true, None::<&str>)?)?;
    menu.append(&MenuItem::with_id(app, "detail", "Open detail window", true, None::<&str>)?)?;

    // When it last ran and when it runs next. The SwiftBar plugin showed only
    // the last poll; the useful half is the next one, because a menu that says
    // "5 min ago" invites a manual poll that would have happened anyway.
    {
        let last = l.last_poll_text.clone().unwrap_or_else(|| "unknown".into());
        match &l.next_poll_text {
            Some(next) => label!("noop_poll", &format!("Polled {last} · next {next}")),
            None => label!("noop_poll", &format!("Polled {last}")),
        }
    }

    // ---- background agent
    let agent = Submenu::new(app, "Background agent", true)?;
    agent.append(&MenuItem::with_id(app, "agent_start", "Start", true, None::<&str>)?)?;
    agent.append(&MenuItem::with_id(app, "agent_stop", "Stop", true, None::<&str>)?)?;
    agent.append(&MenuItem::with_id(app, "agent_restart", "Restart", true, None::<&str>)?)?;
    agent.append(&PredefinedMenuItem::separator(app)?)?;
    agent.append(&MenuItem::with_id(app, "agent_status", "Health check…", true, None::<&str>)?)?;
    agent.append(&MenuItem::with_id(app, "agent_logs", "Live log…", true, None::<&str>)?)?;
    menu.append(&agent)?;

    // ---- alerts, with the current state on the parent so it reads at a glance
    let alerts_on = l.alerts.as_ref().map(|a| a.enabled).unwrap_or(true);
    let threshold = l.alerts.as_ref().and_then(|a| a.threshold_pm25.map(|v| format!("{v:.1} µg/m³"))
        .or_else(|| a.threshold_aqi.map(|v| format!("AQI {}", v.round() as i64))))
        .unwrap_or_else(|| "default".into());
    let alerts = Submenu::new(
        app,
        &format!("Alerts: {} ({})", if alerts_on { "on" } else { "off" }, threshold),
        true)?;
    alerts.append(&MenuItem::with_id(
        app, if alerts_on { "alerts_off" } else { "alerts_on" },
        if alerts_on { "Turn alerts off" } else { "Turn alerts on" },
        true, None::<&str>)?)?;
    alerts.append(&MenuItem::with_id(app, "alerts_test", "Send a test notification", true, None::<&str>)?)?;
    if let Some(a) = &l.alerts {
        if a.quiet_hours.len() == 2 {
            alerts.append(&PredefinedMenuItem::separator(app)?)?;
            alerts.append(&MenuItem::with_id(
                app, "noop_quiet",
                &format!("Quiet {:02}:00–{:02}:00", a.quiet_hours[0], a.quiet_hours[1]),
                false, None::<&str>)?)?;
        }
    }
    alerts.append(&PredefinedMenuItem::separator(app)?)?;
    alerts.append(&MenuItem::with_id(app, "prefs", "Change thresholds…", true, None::<&str>)?)?;
    menu.append(&alerts)?;

    // ---- setup and sources
    let setup = Submenu::new(app, "Setup", true)?;
    // One item, because all three used to open different terminal wizards and
    // now open the same page. Three labels promising three destinations would
    // be a lie about where the click goes; the page has panels for location,
    // sources, keys, alerts, data and backup.
    setup.append(&MenuItem::with_id(app, "settings",
                                    "Settings — location, sources, keys…",
                                    true, None::<&str>)?)?;
    // No "in browser" suffix, unlike the dashboard item: this one opens in
    // Airo's own window, and a label that promised a browser would be a lie
    // about where the click goes.
    let unused: Vec<&airo::NetworkView> = l.networks.iter().filter(|n| !n.in_use).collect();
    if !unused.is_empty() {
        setup.append(&PredefinedMenuItem::separator(app)?)?;
        for n in unused {
            let name = n.label.clone().or_else(|| n.provider.clone())
                .unwrap_or_else(|| "unknown".into());
            let (id, text) = if n.has_key {
                (format!("addsrc_{}", n.provider.clone().unwrap_or_default()),
                 format!("Add {name} — ready"))
            } else {
                (format!("signup_{}", n.provider.clone().unwrap_or_default()),
                 format!("Add {name} — free account needed"))
            };
            setup.append(&MenuItem::with_id(app, &id, &text, true, None::<&str>)?)?;
        }
    }
    menu.append(&setup)?;

    // ---- data
    let data = Submenu::new(app, "Data", true)?;
    data.append(&MenuItem::with_id(app, "backup", "Back up everything…", true, None::<&str>)?)?;
    data.append(&MenuItem::with_id(app, "export", "Export CSV", true, None::<&str>)?)?;
    data.append(&PredefinedMenuItem::separator(app)?)?;
    data.append(&MenuItem::with_id(app, "backfill30", "Backfill 30 days…", true, None::<&str>)?)?;
    data.append(&MenuItem::with_id(app, "backfill365", "Backfill 365 days…", true, None::<&str>)?)?;
    data.append(&PredefinedMenuItem::separator(app)?)?;
    data.append(&MenuItem::with_id(app, "analyse", "Evening analysis…", true, None::<&str>)?)?;
    data.append(&MenuItem::with_id(app, "where", "Where is my data?…", true, None::<&str>)?)?;
    data.append(&MenuItem::with_id(app, "open_data", "Reveal data folder", true, None::<&str>)?)?;
    menu.append(&data)?;

    // ---- attribution. Required by PurpleAir ToS §4.8 and by CC BY for the
    // government feeds. Not optional chrome.
    if !l.attributions.is_empty() {
        menu.append(&PredefinedMenuItem::separator(app)?)?;
        for (i, a) in l.attributions.iter().enumerate() {
            label!(&format!("noop_attr{i}"), a);
        }
    }

    menu.append(&PredefinedMenuItem::separator(app)?)?;
    menu.append(&MenuItem::with_id(app, "quit", "Quit Airo", true, None::<&str>)?)?;
    Ok(menu)
}

fn refresh_tray(app: &tauri::AppHandle) {
    let latest = airo::read_latest().ok();

    if let Some(tray) = app.tray_by_id("airo") {
        match &latest {
            Some(l) => {
                let _ = tray.set_title(Some(&l.tray_title()));
                let _ = tray.set_tooltip(Some(&format!(
                    "Airo — {}",
                    l.provenance.clone().unwrap_or_else(|| "no reading".into()))));
            }
            None => {
                let _ = tray.set_title(Some("⚪ —"));
                let _ = tray.set_tooltip(Some("Airo: no data yet"));
            }
        }
        if let Ok(menu) = build_menu(app, latest.as_ref()) {
            let _ = tray.set_menu(Some(menu));
        }
    }

    if let Some(l) = &latest {
        let _ = app.emit("airo://latest", l);
    }
}

fn show_detail(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// Show settings in Airo's own window.
///
/// Configuring the app should not mean leaving it for a browser tab. The page
/// itself is still served by Python over loopback -- it needs the token the
/// server substitutes, and the validator behind it must stay on the Python
/// side -- so this window is a view onto a local URL rather than bundled
/// content.
///
/// Reused rather than recreated: opening settings twice previously meant two
/// windows, each with its own token-bearing page, and a save from the stale
/// one failing for reasons the user could not see. If the window already
/// exists it is raised, not rebuilt.
fn show_settings(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("settings") {
        let _ = w.show();
        let _ = w.set_focus();
        return;
    }

    // Resolving the URL runs a short Python command, so it happens off the
    // menu thread -- a menu that stalls while a server starts reads as a
    // hang, and starting one can take a couple of seconds on a cold machine.
    let handle = app.clone();
    std::thread::spawn(move || {
        let url = match airo::settings_url() {
            Ok(u) => u,
            Err(e) => {
                // Nothing to show and no window to show it in. The log is
                // where the tray's troubleshooting already points.
                eprintln!("airo: could not open settings: {e}");
                return;
            }
        };
        let parsed = match url.parse() {
            Ok(u) => u,
            Err(e) => {
                eprintln!("airo: settings URL {url} is not usable: {e}");
                return;
            }
        };
        let built = WebviewWindowBuilder::new(
            &handle, "settings", WebviewUrl::External(parsed))
            .title("Airo — settings")
            .inner_size(900.0, 760.0)
            .resizable(true)
            .center()
            .build();
        if let Err(e) = built {
            eprintln!("airo: could not create the settings window: {e}");
        }
    });
}

fn main() {
    // `--print-menu` prints the readout the tray would show and exits. The
    // menu is the product's most-used surface and, until this existed, the
    // only one that could not be checked without a window server: CI is
    // headless, and screen recording is not always permitted even locally.
    // Everything it prints comes from the same readout_lines() the real menu
    // renders, so it cannot drift from what a user sees.
    if std::env::args().any(|a| a == "--print-menu") {
        match airo::read_latest() {
            Ok(l) => {
                for r in airo::readout_lines(&l) {
                    match r {
                        airo::Readout::Separator => println!("  ---"),
                        airo::Readout::Line { text, .. } => println!("  {text}"),
                    }
                }
                println!("  ---");
                println!("  [title] {}", l.tray_title());
                for a in &l.attributions {
                    println!("  [attribution] {a}");
                }
            }
            Err(e) => {
                println!("  no reading yet ({e})");
            }
        }
        return;
    }

    // Before anything visible: a second tray means two icons, two pollers and
    // no way to tell which one a click reached.
    match airo::claim_single_instance() {
        Ok(_) => {}
        Err(why) => {
            eprintln!("airo-tray: {why}");
            std::process::exit(1);
        }
    }

    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![get_latest, poll_now])
        .setup(|app| {
            let handle = app.handle().clone();

            // Everything a fresh install still needs: schedule the background
            // poll, and open the settings page if nothing is configured yet.
            // Python decides all of it -- whether it is a fresh install, what
            // the interval is, whether to open anything -- and the call is
            // safe to repeat, so there is no "have I run before?" state here
            // to get wrong. Spawned, never awaited: the menu must appear even
            // if this is slow or fails.
            if let Err(e) = airo::poller(&["--first-run"]) { airo::report_problem("First-run setup", &e); }

            let menu = build_menu(&handle, airo::read_latest().ok().as_ref())?;

            // macOS draws the icon AND the title, so shipping both put a
            // dark app-icon disc immediately left of the coloured band dot:
            // two circles, one of them meaningless. The title already carries
            // severity, so on macOS the title IS the indicator.
            //
            // Windows and Linux tray items show no title at all, so there the
            // icon is the only thing the user can see and must stay.
            let builder = TrayIconBuilder::with_id("airo")
                .menu(&menu);
            #[cfg(not(target_os = "macos"))]
            let builder = builder.icon(app.default_window_icon().unwrap().clone());
            builder
                // macOS convention for a title-bearing tray item is that a
                // left click opens the menu.
                .show_menu_on_left_click(true)
                .on_menu_event(move |app, event| {
                    let id = event.id().as_ref().to_string();
                    // Non-clickable rows carry the reading itself.
                    if id.starts_with("noop_") {
                        return;
                    }
                    match id.as_str() {
                        "quit" => app.exit(0),
                        "refresh" => refresh_tray(app),
                        "detail" => show_detail(app),
                        "poll" => {
                            if let Err(e) = airo::poll_now() { airo::report_problem("Fetch a new reading", &e); }
                        }
                        "dashboard" => {
                            if let Err(e) = airo::open_dashboard() { airo::report_problem("Open dashboard in browser", &e); }
                        }
                        "agent_start" => { if let Err(e) = airo::scheduler("start") { airo::report_problem("Start the background agent", &e); } }
                        "agent_stop" => { if let Err(e) = airo::scheduler("stop") { airo::report_problem("Stop the background agent", &e); } }
                        "agent_restart" => { if let Err(e) = airo::scheduler("restart") { airo::report_problem("Restart the background agent", &e); } }
                        "agent_status" => { if let Err(e) = airo::poller(&["--doctor"]) { airo::report_problem("Check the background agent", &e); } }
                        "agent_logs" => { if let Err(e) = airo::poller(&["--logs"]) { airo::report_problem("Open the logs", &e); } }
                        "alerts_on" => {
                            if let Err(e) = airo::set_alerts(true) { airo::report_problem("Turn alerts on", &e); }
                            refresh_tray(app);
                        }
                        "alerts_off" => {
                            if let Err(e) = airo::set_alerts(false) { airo::report_problem("Turn alerts off", &e); }
                            refresh_tray(app);
                        }
                        "alerts_test" => { if let Err(e) = airo::poller(&["--test-alert"]) { airo::report_problem("Send a test notification", &e); } }
                        // These all used to spawn a Terminal running an
                        // interactive wizard. They are one settings page now.
                        // The old ids stay routed: a menu built by an older
                        // binary, or a click landing mid-refresh, must not
                        // silently do nothing.
                        "settings" | "setup" | "keys" | "prefs" => {
                            show_settings(app);
                        }
                        "backup" => { show_settings(app); }
                        "analyse" => { if let Err(e) = airo::open_dashboard() { airo::report_problem("Open dashboard in browser", &e); } }
                        "where" => { show_settings(app); }
                        // Backfills take minutes; run them where the user can
                        // see progress rather than appearing to do nothing.
                        "backfill30" => { if let Err(e) = airo::poller(&["--backfill", "30"]) { airo::report_problem("Backfill 30 days", &e); } }
                        "backfill365" => { if let Err(e) = airo::poller(&["--backfill", "365"]) { airo::report_problem("Backfill a year", &e); } }
                        "export" => { show_settings(app); }
                        other if other.starts_with("signup_") => {
                            // Open the provider's registration page. The URL
                            // comes from latest.json, which the poller wrote,
                            // so the tray still decides nothing.
                            if let Ok(l) = airo::read_latest() {
                                let slug = other.trim_start_matches("signup_");
                                if let Some(n) = l.networks.iter()
                                    .find(|n| n.provider.as_deref() == Some(slug)) {
                                    if let Some(u) = &n.signup_url {
                                        if let Err(e) = airo::open_url_public(u) { airo::report_problem("Open the signup page", &e); }
                                    }
                                }
                            }
                        }
                        other if other.starts_with("addsrc_") => {
                            // "Add a site from this network" used to open a
                            // terminal wizard. The settings page has a Sources
                            // panel that searches and adds, so it goes there.
                            show_settings(app);
                        }
                        "open_data" => { if let Err(e) = airo::reveal("data") { airo::report_problem("Show the data folder", &e); } }
                        _ => {}
                    }
                })
                .build(app)?;

            refresh_tray(&handle);

            // Poll the file, not the network. Cheap enough to be frequent.
            tauri::async_runtime::spawn(async move {
                loop {
                    tokio::time::sleep(Duration::from_secs(REFRESH_SECS)).await;
                    refresh_tray(&handle);
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window hides it; the tray stays. Quitting is an
            // explicit menu action, because an accidental close should not
            // silently stop the readout.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Airo tray");
}
