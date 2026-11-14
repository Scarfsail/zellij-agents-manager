use serde::Deserialize;
use std::collections::{BTreeMap, HashMap};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};
use zellij_tile::prelude::*;

// ---------- data from the scanner ----------

#[derive(Deserialize, Default, Clone)]
struct Scan {
    now: i64,
    agents: Vec<Agent>,
    /// account usage per vendor ("claude"), from the scanner's cache of the vendor's usage endpoint
    #[serde(default)]
    usage: BTreeMap<String, VendorUsage>,
}

#[derive(Deserialize, Clone, Default)]
#[serde(default)]
struct VendorUsage {
    credits: Option<Credits>,
    five_hour: Option<Window>,
    seven_day: Option<Window>,
    /// named limits (agy's quota buckets), shown after the 5h and weekly ones
    windows: Vec<Window>,
    error: Option<String>,
}

/// usage-based spend this month (enterprise)
#[derive(Deserialize, Clone, Default)]
#[serde(default)]
struct Credits {
    used: f64,
    limit: f64,
    pct: Option<f64>,
    currency: Option<String>,
    resets_at: i64,
    /// local time zone offset at `resets_at`: the plugin has none of its own
    utc_offset_s: i64,
}

/// a subscription's rolling limit (5h, week, or a named one)
#[derive(Deserialize, Clone, Default)]
#[serde(default)]
struct Window {
    label: String,
    pct: Option<f64>,
    resets_at: i64,
    utc_offset_s: i64,
}

#[derive(Deserialize, Clone, Default)]
#[serde(default)]
struct Agent {
    kind: String,
    pid: i64,
    id: Option<String>,
    name: String,
    cwd: Option<String>,
    started_at: i64,
    status: String,
    status_since: i64,
    detail: String,
    zellij_session: Option<String>,
    pane_id: Option<u32>,
    ctx_tokens: Option<u64>,
    ctx_pct: Option<f64>,
    title: String,
    branch: Option<String>,
    model: Option<String>,
    /// session cost so far, from Claude's status line
    cost_usd: Option<f64>,
    /// the current question was already looked at (scanner's shared "read" mark)
    seen: bool,
}

// ---------- plugin state ----------

#[derive(PartialEq, Clone, Copy)]
enum Mode {
    Bar,
    Panel,
}

struct Row {
    agent: Agent,
    tab_pos: Option<usize>,
    pane_title: String,
    local: bool,
    focused: bool,
    /// waiting on you, but you have already focused it since the question came in
    seen: bool,
    /// (floating, y, x) on screen: numbers go top-to-bottom, left-to-right within a tab
    screen_pos: (bool, usize, usize),
}

/// Bar: the floating panel this bar opened, and the floating panes it hid to show the panel alone
struct Panel {
    id: u32,
    /// listed in a PaneUpdate at least once: absent before that means "not yet listed", not "closed"
    seen: bool,
    stashed: Vec<PaneId>,
}

struct State {
    mode: Mode,
    /// our layout config, passed on (with mode=panel) to the panel the header toggles
    cfg: BTreeMap<String, String>,
    scanner: String,
    interval: f64,
    hide_on_jump: bool,
    /// the bar asks the scanner for system notifications on status changes
    notify: bool,
    debug_dump: Option<String>,
    session: Option<String>,
    tabs: Vec<TabInfo>,
    panes: PaneManifest,
    scan: Scan,
    rows: Vec<Row>,
    selected: usize,
    // clickable regions from the last render: (line, col_start, col_end, row index)
    hits: Vec<(usize, usize, usize, usize)>,
    /// width of the bar's header block from the last render (clicking it toggles the panel)
    header_w: usize,
    panel: Option<Panel>,
    permitted: bool,
    scanning: bool,
    my_id: u32,
    my_tab: Option<usize>,
    ticks: u64,
    last_scan_tick: u64,
    /// spinner frame for working agents, derived from the clock so stray ticks can't speed it up
    frame: usize,
    started: std::time::Instant,
    /// when our one timer is due; earlier Timer events belong to duplicate chains and are dropped
    next_tick: Option<std::time::Instant>,
    /// seconds of animation ticks since the last poll (polls still happen once per `interval`)
    since_poll: f64,
    hidden: bool,
    /// terminal panes the attached clients are focused on (authoritative; PaneInfo.is_focused goes stale)
    client_focus: Vec<u32>,
    /// client_focus came from a live ListClients report (not handed over or kept while on the panel): only then mark questions read
    focus_live: bool,
    /// Panel: start the selection on the focused agent once a row is focused, unless the user moved it first
    select_focused: bool,
    /// running zellij sessions (SessionUpdate); None until the first update arrives
    live_sessions: Option<Vec<String>>,
    /// SGR for the focused chip, from zellij's theme (ribbon_selected = the active tab's colours)
    active_style: Option<String>,
    /// questions marked read by this instance (session id, status_since), until the scanner reports it
    seen_local: std::collections::HashSet<(String, i64)>,
    error: Option<String>,
}

impl Default for State {
    fn default() -> Self {
        Self {
            mode: Mode::Bar,
            cfg: BTreeMap::new(),
            scanner: "zam-scan.py".into(),
            interval: 2.0,
            hide_on_jump: true,
            notify: true,
            debug_dump: None,
            session: None,
            tabs: vec![],
            panes: PaneManifest::default(),
            scan: Scan::default(),
            rows: vec![],
            selected: 0,
            hits: vec![],
            header_w: 0,
            panel: None,
            permitted: false,
            scanning: false,
            my_id: 0,
            my_tab: None,
            ticks: 1,
            last_scan_tick: 0,
            frame: 0,
            started: std::time::Instant::now(),
            next_tick: None,
            since_poll: 0.0,
            hidden: false,
            client_focus: Vec::new(),
            focus_live: false,
            select_focused: false,
            live_sessions: None,
            active_style: None,
            seen_local: Default::default(),
            error: None,
        }
    }
}

register_plugin!(State);

impl ZellijPlugin for State {
    fn load(&mut self, cfg: BTreeMap<String, String>) {
        if cfg.get("mode").map(|m| m == "panel").unwrap_or(false) {
            self.mode = Mode::Panel;
            // while the panel is open the client is focused on it, so the bar hands over what was focused before
            self.client_focus = cfg.get("focus").into_iter().flat_map(|f| f.split(',')).filter_map(|id| id.parse().ok()).collect();
            self.select_focused = true;
        }
        if let Some(s) = cfg.get("scanner") {
            self.scanner = s.clone();
        }
        if let Some(i) = cfg.get("interval").and_then(|v| v.parse().ok()) {
            self.interval = i;
        }
        self.debug_dump = cfg.get("debug_dump").cloned();
        if let Some(h) = cfg.get("hide_on_jump") {
            self.hide_on_jump = h != "false";
        }
        if let Some(n) = cfg.get("notify") {
            self.notify = n != "false";
        }
        self.cfg = cfg;
        self.my_id = get_plugin_ids().plugin_id;
        request_permission(&[
            PermissionType::ReadApplicationState,
            PermissionType::ChangeApplicationState,
            PermissionType::RunCommands,
            PermissionType::OpenTerminalsOrPlugins,
        ]);
        subscribe(&[
            EventType::PermissionRequestResult,
            EventType::ModeUpdate,
            EventType::TabUpdate,
            EventType::PaneUpdate,
            EventType::Timer,
            EventType::RunCommandResult,
            EventType::Mouse,
            EventType::Key,
            EventType::ListClients,
            EventType::SessionUpdate,
        ]);
    }

    fn update(&mut self, event: Event) -> bool {
        match event {
            Event::PermissionRequestResult(PermissionStatus::Granted) => {
                self.permitted = true;
                if self.mode == Mode::Bar {
                    set_selectable(false);
                }
                self.kick_scan();
                if self.next_tick.is_none() {
                    self.arm(self.interval); // Granted can arrive more than once: never start a second chain
                }
                true
            }
            Event::PermissionRequestResult(PermissionStatus::Denied) => {
                self.error = Some("permissions denied".into());
                true
            }
            Event::ModeUpdate(mi) => {
                let rs = &mi.style.colors.ribbon_selected;
                let style = Some(format!("\x1b[{};{}m", sgr(rs.base, 38), sgr(rs.background, 48)));
                let changed = self.session != mi.session_name || self.active_style != style;
                self.active_style = style;
                self.session = mi.session_name;
                if changed {
                    self.rebuild();
                }
                changed
            }
            Event::TabUpdate(tabs) => {
                self.tabs = tabs;
                self.rebuild();
                list_clients(); // tab switch: re-check visibility right away
                true
            }
            Event::PaneUpdate(pm) => {
                self.panes = pm;
                self.my_tab = self.my_pane().map(|(t, _)| t);
                self.track_panel();
                self.rebuild();
                list_clients();
                true
            }
            Event::Timer(_) => {
                let now = std::time::Instant::now();
                if self.next_tick.is_some_and(|t| now + std::time::Duration::from_millis(50) < t) {
                    return false; // a duplicate chain firing early: let it die
                }
                // One timer chain: fast while a visible agent is working (spinner), else once per interval.
                let animate = self.animating();
                let step = if animate { ANIM_STEP } else { self.interval };
                self.arm(step);
                self.since_poll += step;
                if !animate || self.since_poll >= self.interval {
                    // Visibility check first; the scan happens in ListClients if we are on screen.
                    self.since_poll = 0.0;
                    self.ticks += 1;
                    list_clients();
                }
                if animate {
                    let frame = (self.started.elapsed().as_secs_f64() / ANIM_STEP) as usize;
                    let changed = frame != self.frame;
                    self.frame = frame;
                    return changed;
                }
                false
            }
            Event::ListClients(clients) => {
                let was_hidden = self.hidden;
                self.hidden = self.hidden_from(&clients);
                if !self.hidden && (was_hidden || self.due()) {
                    self.kick_scan();
                }
                let focus: Vec<u32> = clients
                    .iter()
                    .filter_map(|c| match c.pane_id {
                        PaneId::Terminal(id) => Some(id),
                        _ => None,
                    })
                    .collect();
                // a client on the panel reports no terminal: the panel keeps the focus the bar handed over, the bar keeps its own;
                // kept focus is for display only, so it never marks questions read
                let on_panel = self.panel.as_ref().is_some_and(|p| clients.iter().any(|c| c.pane_id == PaneId::Plugin(p.id)));
                let keep = focus.is_empty() && (self.mode == Mode::Panel || on_panel);
                let changed = !keep && (focus != self.client_focus || !self.focus_live);
                self.focus_live = !keep;
                if changed {
                    self.client_focus = focus;
                    self.rebuild();
                }
                self.track_panel(); // a panel that closed while our tab was off screen restores once it is back
                changed
            }
            Event::SessionUpdate(sessions, _) => {
                self.live_sessions = Some(sessions.into_iter().map(|s| s.name).collect());
                false
            }
            Event::RunCommandResult(code, out, err, ctx) => {
                match ctx.get("zam").map(String::as_str) {
                    Some("scan") => {}
                    _ => return false,
                }
                self.scanning = false;
                if code != Some(0) {
                    self.error = Some(format!(
                        "scanner failed: {}",
                        String::from_utf8_lossy(&err).lines().last().unwrap_or("?")
                    ));
                    return true;
                }
                match serde_json::from_slice::<Scan>(&out) {
                    Ok(scan) => {
                        self.scan = scan;
                        self.error = None;
                    }
                    Err(e) => self.error = Some(format!("bad scanner output: {e}")),
                }
                self.rebuild();
                true
            }
            Event::Mouse(Mouse::LeftClick(line, col)) => {
                if line < 0 {
                    return false;
                }
                let (line, col) = (line as usize, col);
                if self.mode == Mode::Bar && line < 2 && col < self.header_w {
                    self.toggle_panel();
                    return false;
                }
                if let Some(&(_, _, _, idx)) =
                    self.hits.iter().find(|(l, a, b, _)| *l == line && col >= *a && col < *b)
                {
                    self.selected = idx;
                    self.jump(idx);
                }
                true
            }
            Event::Mouse(Mouse::ScrollDown(_)) => self.move_sel(1),
            Event::Mouse(Mouse::ScrollUp(_)) => self.move_sel(-1),
            Event::Key(k) if self.mode == Mode::Panel => match k.bare_key {
                BareKey::Down | BareKey::Char('j') => self.move_sel(1),
                BareKey::Up | BareKey::Char('k') => self.move_sel(-1),
                BareKey::Enter => {
                    self.jump(self.selected);
                    true
                }
                BareKey::Char('n') => {
                    // jump to the next agent that needs attention (needs_input first, then idle)
                    if let Some(i) = self.next_attention() {
                        self.selected = i;
                        self.jump(i);
                    }
                    true
                }
                BareKey::Char('r') => {
                    self.kick_scan();
                    false
                }
                BareKey::Esc | BareKey::Char('q') => {
                    close_self();
                    false
                }
                _ => false,
            },
            _ => false,
        }
    }

    /// Keybindings broadcast `MessagePlugin { name "zam_jump"; payload "<n>" }` to every plugin;
    /// only the instance on screen acts, so each press jumps once.
    fn pipe(&mut self, msg: PipeMessage) -> bool {
        // my_tab None = this instance hasn't learned where it is yet (e.g. never-visited tab): don't act
        // the panel shares the bar's tab, so only the bar acts (else each press would jump twice)
        if msg.name != "zam_jump" || self.mode != Mode::Bar || self.hidden || self.my_tab.is_none() || !self.permitted {
            return false;
        }
        match msg.payload.as_deref().map(str::trim) {
            Some("panel") => self.toggle_panel(),
            Some("next") => {
                if let Some(i) = self.next_attention() {
                    self.selected = i;
                    self.jump(i);
                }
            }
            // cycle through the bar order from the focused agent, wrapping around at both ends
            Some(dir @ ("left" | "right")) if !self.rows.is_empty() => {
                let n = self.rows.len();
                let i = match (self.rows.iter().position(|r| r.focused), dir) {
                    (Some(f), "right") => (f + 1) % n,
                    (Some(f), _) => (f + n - 1) % n,
                    (None, "right") => 0,
                    (None, _) => n - 1,
                };
                self.selected = i;
                self.jump(i);
            }
            Some(n) => {
                if let Some(i) = n.parse::<usize>().ok().filter(|n| *n >= 1) {
                    self.jump(i - 1);
                }
            }
            None => {}
        }
        self.error.is_some() // redraw so "session … is not running" shows
    }

    fn render(&mut self, rows: usize, cols: usize) {
        self.hits.clear();
        self.header_w = 0;
        let frame = match self.mode {
            Mode::Bar => self.render_bar(cols),
            Mode::Panel => self.render_panel(rows, cols),
        };
        print!("{frame}");
        if let Some(path) = &self.debug_dump {
            let plugin_panes: Vec<String> = self
                .panes
                .panes
                .iter()
                .flat_map(|(t, ps)| ps.iter().filter(|p| p.is_plugin).map(move |p| format!("{t}:{}", p.id)))
                .collect();
            let dbg = format!(
                "{frame}\n[dbg my_id={} my_tab={:?} hidden={} plugin_panes={plugin_panes:?}]\n",
                self.my_id, self.my_tab, self.hidden
            );
            let path = path.replace("{id}", &self.my_id.to_string());
            let _ = std::fs::write(path, dbg);
        }
    }
}

// ---------- logic ----------

impl State {
    fn tab_of(&self, pane: &PaneId) -> Option<usize> {
        let (id, plugin) = match pane {
            PaneId::Terminal(id) => (*id, false),
            PaneId::Plugin(id) => (*id, true),
        };
        self.panes
            .panes
            .iter()
            .find(|(_, ps)| ps.iter().any(|p| p.is_plugin == plugin && p.id == id))
            .map(|(t, _)| *t)
    }

    /// Only the instance in a tab some client is looking at scans; hidden tabs idle.
    /// (TabInfo.active goes stale for plugins in hidden tabs, and hidden plugins
    /// still render, so neither can tell visibility.)
    /// Zellij drops clients focused on the requesting plugin's own pane from `list_clients()`
    /// (`remove_plugin_from_layout`, "to prevent deadlock"), so for the panel a missing current
    /// client is on us. Bars are not selectable and never take focus, so the exception is panel-only;
    /// for a bar a missing current client (e.g. no client attached, `[]`) means hidden.
    fn hidden_from(&self, clients: &[ClientInfo]) -> bool {
        match self.my_tab {
            Some(mine) => {
                let on_us_missing = self.mode == Mode::Panel && !clients.iter().any(|c| c.is_current_client);
                !on_us_missing && !clients.iter().any(|c| self.tab_of(&c.pane_id) == Some(mine))
            }
            None => false,
        }
    }

    /// our own plugin pane and the tab it sits in
    fn my_pane(&self) -> Option<(usize, &PaneInfo)> {
        self.panes
            .panes
            .iter()
            .find_map(|(t, ps)| ps.iter().find(|p| p.is_plugin && p.id == self.my_id).map(|p| (*t, p)))
    }

    /// our tab's TabInfo
    fn my_tab_info(&self) -> Option<&TabInfo> {
        self.tabs.iter().find(|ti| Some(ti.position) == self.my_tab)
    }

    /// Bar: close our panel if it is open, else open it (this plugin, our config with mode=panel) pinned, full width,
    /// right below the bar and sized to the agents. Zellij shows a tab's floating panes only as a whole layer, so if
    /// that layer was hidden, the other floating panes it brings up are hidden again until the panel closes.
    fn toggle_panel(&mut self) {
        if !self.permitted {
            return;
        }
        if let Some(p) = &mut self.panel {
            close_plugin_pane(p.id);
            p.seen = true; // closing: its next absence from the manifest is final (track_panel restores)
            return;
        }
        let Some((tab, y, Some(url))) = self.my_pane().map(|(t, p)| (t, p.pane_y + p.pane_rows, p.plugin_url.clone()))
        else {
            return;
        };
        let Some(ti) = self.my_tab_info() else { return };
        let layer_hidden = !ti.are_floating_panes_visible;
        let height = (Self::panel_header_lines(self.usage_lines(ti.viewport_columns).len()) + self.rows.len().max(1) + PANEL_FOOTER_LINES + 2).min(ti.viewport_rows / 2).max(6);
        let mut cfg = self.cfg.clone();
        cfg.insert("mode".into(), "panel".into());
        cfg.insert("focus".into(), self.client_focus.iter().map(u32::to_string).collect::<Vec<_>>().join(","));
        let coords = FloatingPaneCoordinates::new(
            Some("0".into()),
            Some(y.to_string()),
            Some("100%".into()),
            Some(height.to_string()),
            Some(true),
            None,
        );
        let Some(PaneId::Plugin(id)) = open_plugin_pane_floating(&url, cfg, coords, BTreeMap::new()) else {
            self.error = Some("panel: could not open".into());
            return;
        };
        let mut stashed = vec![];
        if layer_hidden {
            let others = self.panes.panes.get(&tab).into_iter().flatten();
            for p in others.filter(|p| p.is_floating && !p.is_suppressed && !(p.is_plugin && p.id == id)) {
                let pid = if p.is_plugin { PaneId::Plugin(p.id) } else { PaneId::Terminal(p.id) };
                hide_pane_with_id(pid);
                stashed.push(pid);
            }
        }
        self.panel = Some(Panel { id, seen: false, stashed });
    }

    /// Bar, on PaneUpdate and ListClients: once our panel is gone (toggle, q/esc, jump, closed by hand), bring back
    /// the floating panes it hid and hide the floating layer again, as it was before, unless a client is now focused
    /// on a floating pane of our tab. Waits while our tab is off screen: show_pane_with_id would switch to it.
    fn track_panel(&mut self) {
        let Some(p) = &mut self.panel else { return };
        let listed = self.panes.panes.values().flatten().any(|q| q.is_plugin && q.id == p.id);
        if listed || !p.seen {
            p.seen |= listed;
            return;
        }
        if self.hidden {
            return;
        }
        let Some(p) = self.panel.take() else { return };
        for &pid in &p.stashed {
            show_pane_with_id(pid, true, false);
        }
        let Some((tab, tab_id)) = self.my_tab.zip(self.my_tab_info().map(|ti| ti.tab_id)) else { return };
        let floating_focus = self.panes.panes.get(&tab).into_iter().flatten().any(|q| {
            q.is_floating && !q.is_plugin && self.client_focus.contains(&q.id)
        });
        if !p.stashed.is_empty() && !floating_focus {
            let _ = hide_floating_panes(Some(tab_id));
        }
    }

    fn arm(&mut self, step: f64) {
        set_timeout(step);
        self.next_tick = Some(std::time::Instant::now() + std::time::Duration::from_secs_f64(step));
    }

    /// Redraw the spinner only on screen, and only when something is spinning.
    fn animating(&self) -> bool {
        !self.hidden && self.rows.iter().any(|r| r.agent.status == "busy" || r.agent.status == "needs_input")
    }

    /// list_clients() fires on every pane/tab event too; keep scans at most once per interval.
    fn due(&self) -> bool {
        self.last_scan_tick != self.ticks
    }

    fn kick_scan(&mut self) {
        if !self.permitted || self.scanning {
            return;
        }
        self.scanning = true;
        self.last_scan_tick = self.ticks;
        // the scanner ignores --from; it shows up in `ps` and helps debugging
        let who = format!("id={}", self.my_id);
        let mut args = vec!["--from", who.as_str()];
        // only bars notify: the panel is transient and would ignore the bar's `notify false`
        if self.mode == Mode::Bar && self.notify {
            args.push("--notify");
        }
        self.run_scanner(&args, "scan");
    }

    fn run_scanner(&self, args: &[&str], tag: &str) {
        let mut ctx = BTreeMap::new();
        ctx.insert("zam".to_string(), tag.to_string());
        // run_command has no shell, so expand a leading "~/" here (via sh, which knows $HOME)
        if let Some(rest) = self.scanner.strip_prefix("~/") {
            let script = r#"f="$HOME/$1"; shift; exec python3 -I "$f" "$@""#;
            let mut cmd = vec!["sh", "-c", script, "sh", rest];
            cmd.extend_from_slice(args);
            run_command(&cmd, ctx);
        } else {
            let mut cmd = vec!["python3", "-I", self.scanner.as_str()];
            cmd.extend_from_slice(args);
            run_command(&cmd, ctx);
        }
    }

    fn rebuild(&mut self) {
        let mut where_: HashMap<u32, (usize, String, (bool, usize, usize))> = HashMap::new();
        for (tab_pos, panes) in &self.panes.panes {
            for p in panes.iter().filter(|p| !p.is_plugin) {
                where_.insert(p.id, (*tab_pos, p.title.clone(), (p.is_floating, p.pane_y, p.pane_x)));
            }
        }
        let mut rows: Vec<Row> = self
            .scan
            .agents
            .iter()
            .map(|a| {
                let local = a.zellij_session.is_some() && a.zellij_session == self.session;
                let loc = if local { a.pane_id.and_then(|id| where_.get(&id)) } else { None };
                let tab_pos = loc.map(|l| l.0);
                Row {
                    pane_title: loc.map(|l| l.1.clone()).unwrap_or_default(),
                    focused: loc.is_some() && a.pane_id.map_or(false, |id| self.client_focus.contains(&id)),
                    tab_pos,
                    screen_pos: loc.map(|l| l.2).unwrap_or_default(),
                    local,
                    seen: a.seen
                        || a.id.as_ref().is_some_and(|id| self.seen_local.contains(&(id.clone(), a.status_since))),
                    agent: a.clone(),
                }
            })
            .collect();
        // local agents first, in tab order; then other sessions; then agents outside zellij
        rows.sort_by_key(|r| {
            (
                !r.local,
                r.agent.zellij_session.is_none(),
                r.tab_pos.unwrap_or(usize::MAX),
                r.screen_pos,
                r.agent.started_at,
            )
        });
        // focusing a waiting agent reads its question: shared through the scanner so every tab's bar agrees
        for r in rows.iter_mut().filter(|r| self.focus_live && r.focused && r.agent.status == "needs_input" && !r.seen) {
            r.seen = true;
            if let Some(id) = r.agent.id.clone() {
                let ts = r.agent.status_since.to_string();
                self.run_scanner(&["--seen", &id, &ts], "seen");
                self.seen_local.insert((id, r.agent.status_since));
            }
        }
        self.seen_local.retain(|k| rows.iter().any(|r| r.agent.id.as_ref() == Some(&k.0) && r.agent.status_since == k.1));
        self.rows = rows;
        if self.selected >= self.rows.len() {
            self.selected = self.rows.len().saturating_sub(1);
        }
        if self.select_focused {
            if let Some(i) = self.rows.iter().position(|r| r.focused) {
                self.selected = i;
                self.select_focused = false;
            }
        }
    }

    fn move_sel(&mut self, d: isize) -> bool {
        self.select_focused = false; // the user's choice wins over a late focus match
        if self.rows.is_empty() {
            return false;
        }
        let n = self.rows.len() as isize;
        self.selected = ((self.selected as isize + d).rem_euclid(n)) as usize;
        true
    }

    fn next_attention(&self) -> Option<usize> {
        let n = self.rows.len();
        for want in ["needs_input", "idle"] {
            for off in 1..=n {
                let i = (self.selected + off) % n;
                if self.rows[i].agent.status == want && !self.rows[i].focused {
                    return Some(i);
                }
            }
        }
        None
    }

    fn jump(&mut self, idx: usize) {
        self.select_focused = false; // click, enter and `n` pick a row too
        let Some(row) = self.rows.get(idx) else { return };
        let Some(pane) = row.agent.pane_id else { return };
        if row.local {
            focus_terminal_pane(pane, true, false); // a hidden floating pane comes back floating
        } else if let Some(s) = &row.agent.zellij_session {
            // switching to a name that isn't running would silently create an empty session
            if self.live_sessions.as_ref().map_or(true, |live| live.contains(s)) {
                switch_session_with_focus(s, None, Some((pane, false)));
            } else {
                self.error = Some(format!("session {s} is not running"));
                return;
            }
        }
        if self.mode == Mode::Panel && self.hide_on_jump {
            close_self();
        }
    }

    fn counts(&self) -> (usize, usize, usize, usize) {
        let mut c = (0, 0, 0, 0); // needs, busy, idle, total
        for r in &self.rows {
            match r.agent.status.as_str() {
                "needs_input" => c.0 += 1,
                "busy" | "shell" => c.1 += 1,
                "idle" => c.2 += 1,
                _ => {}
            }
            c.3 += 1;
        }
        c
    }
}

// ---------- rendering ----------

const RESET: &str = "\x1b[0m";
const DIM: &str = "\x1b[2m";
const BOLD: &str = "\x1b[1m";
const REV: &str = "\x1b[7m";
/// dark grey background: the bar's unfocused chips and the panel's selected row
const GREY_BG: &str = "\x1b[48;5;236m";

/// SGR parameters for a theme colour; `layer` is 38 (fg) or 48 (bg).
fn sgr(c: PaletteColor, layer: u8) -> String {
    match c {
        PaletteColor::Rgb((r, g, b)) => format!("{layer};2;{r};{g};{b}"),
        PaletteColor::EightBit(n) => format!("{layer};5;{n}"),
    }
}

/// Waiting-for-you colours: amber (not red, which reads as an error). The chip pulses between the two.
const WAIT_FG: &str = "\x1b[1;38;5;214m";
const WAIT_BG: &str = "\x1b[1;30;48;5;214m";

/// Spinner for working agents: a quarter-filled circle turning clockwise.
const SPINNER: [&str; 4] = ["◐", "◓", "◑", "◒"];
/// Seconds per spinner frame.
const ANIM_STEP: f64 = 0.3;

fn status_style(status: &str, frame: usize) -> (&'static str, &'static str, &'static str) {
    // (icon, label, colour)
    match status {
        "needs_input" => ("?", "NEEDS YOU", WAIT_FG),
        "busy" => (SPINNER[frame % SPINNER.len()], "working", "\x1b[33m"),
        "shell" => ("●", "shell", "\x1b[34m"),
        "idle" => ("○", "idle", "\x1b[32m"),
        _ => ("?", "unknown", "\x1b[90m"),
    }
}

fn ago(now: i64, ts: i64) -> String {
    if ts <= 0 || now <= 0 {
        return "-".into();
    }
    let s = ((now - ts) / 1000).max(0);
    match s {
        0..=59 => format!("{s}s"),
        60..=3599 => format!("{}m", s / 60),
        3600..=86399 => format!("{}h{:02}m", s / 3600, (s % 3600) / 60),
        _ => format!("{}d", s / 86400),
    }
}

/// Terminal cell width (emoji such as ✅ take 2 cells), matching how zellij lays text out.
fn dw(s: &str) -> usize {
    UnicodeWidthStr::width(s)
}

/// Fit `s` into exactly `w` cells: pad with spaces, or cut and end with "…".
fn trunc(s: &str, w: usize) -> String {
    if dw(s) <= w {
        return format!("{s}{}", " ".repeat(w - dw(s)));
    }
    if w == 0 {
        return String::new();
    }
    let mut t = String::new();
    let mut used = 0;
    for c in s.chars() {
        let cw = UnicodeWidthChar::width(c).unwrap_or(0);
        if used + cw > w - 1 {
            break;
        }
        t.push(c);
        used += cw;
    }
    t.push('…');
    t.push_str(&" ".repeat(w - 1 - used));
    t
}

/// Widest first: the largest cap c ≥ `floor` at which Σ max(lo, min(nat, c)) ≤ `budget`, over (lo, nat) items
/// (`lo` never shrinks, `nat` is the natural width). The widest natural width when all fit; None if `floor` doesn't.
fn shared_cap(items: &[(usize, usize)], budget: usize, floor: usize) -> Option<usize> {
    let top = items.iter().map(|&(_, nat)| nat).max().unwrap_or(0);
    (floor.min(top)..=top).rev().find(|&c| items.iter().map(|&(lo, nat)| lo.max(nat.min(c))).sum::<usize>() <= budget)
}

fn short_model(m: &Option<String>) -> String {
    let m = m.as_deref().unwrap_or("");
    m.trim_start_matches("claude-").to_string()
}

/// Claude sets the pane title to "<spinner> task summary"; drop the spinner glyph.
fn clean_title(t: &str) -> String {
    let t = t.trim();
    match t.chars().next() {
        Some(c) if !c.is_alphanumeric() && !c.is_ascii_punctuation() => t[c.len_utf8()..].trim().to_string(),
        _ => t.to_string(),
    }
}

impl State {
    /// Exact % from Claude's statusline JSON; never guessed from a window size.
    fn ctx_pct(&self, a: &Agent) -> Option<u64> {
        a.ctx_pct.map(|p| p.round().max(0.0) as u64)
    }

    /// "ctx 78%" when known, otherwise raw tokens "ctx 156k" (no percentage without the real window).
    fn ctx_label(&self, a: &Agent) -> Option<String> {
        match (self.ctx_pct(a), a.ctx_tokens) {
            (Some(p), _) => Some(format!("{p}%")),
            (None, Some(t)) => Some(format!("{}k", (t + 500) / 1000)),
            _ => None,
        }
    }

    /// "eng-intelligence@feat/168…" — repo (cwd basename) + git branch.
    fn location(&self, r: &Row) -> String {
        let repo = r.agent.cwd.as_deref().and_then(|c| c.rsplit('/').find(|p| !p.is_empty())).unwrap_or(&r.agent.name);
        match r.agent.branch.as_deref() {
            Some(b) if !b.is_empty() && b != "HEAD" => format!("{repo}@{b}"),
            _ => repo.to_string(),
        }
    }

    /// Claude's session title (custom > ai-title), else the pane title, else the derived name.
    fn purpose(&self, r: &Row) -> String {
        if !r.agent.title.is_empty() {
            return r.agent.title.clone();
        }
        let t = clean_title(&r.pane_title);
        if !t.is_empty() && !t.starts_with("Pane #") {
            return t;
        }
        r.agent.name.clone()
    }

    /// The tab's name; `↗session` for an agent in another zellij session.
    fn tab_tag(&self, r: &Row) -> String {
        match (r.tab_pos, &r.agent.zellij_session) {
            (Some(p), _) => self.tabs.iter().find(|t| t.position == p).map_or_else(|| format!("{}", p + 1), |t| t.name.clone()),
            (None, Some(s)) if !r.local => format!("↗{s}"),
            _ => "?".into(),
        }
    }

    /// Two-row bar. Row 0: purpose. Row 1: status icon, number, location, ctx. Chips split by │.
    fn render_bar(&mut self, cols: usize) -> String {
        // chips shrink (widest first) down to this; below it, trailing chips hide behind "+N"
        const CHIP_MIN: usize = 16;
        let (needs, busy, idle, total) = self.counts();
        let mut l0 = String::new();
        let mut l1 = String::new();
        let mut x = 0usize;
        // header block
        let h0 = format!(" AGENTS {total} ");
        let h1 = format!(" ?{needs} ●{busy} ○{idle} ");
        let hw = dw(&h0).max(dw(&h1));
        l0.push_str(&format!("{BOLD}{}{RESET}", trunc(&h0, hw)));
        let needs_style = if needs > 0 { WAIT_FG } else { DIM };
        l1.push_str(&format!("{DIM} {needs_style}?{needs}{RESET}{DIM} \x1b[33m●{busy}{RESET}{DIM} \x1b[32m○{idle}{RESET}{}", " ".repeat(hw - dw(&h1) + 1)));
        x += hw;
        self.header_w = hw;
        if let Some(e) = &self.error {
            let e = trunc(e, cols.saturating_sub(x + 3).min(60));
            let w = dw(&e);
            l0.push_str(&format!("{DIM}│{RESET} {} ", " ".repeat(w)));
            l1.push_str(&format!("{DIM}│{RESET} {RED}{e}{RESET} "));
            x += w + 3;
        }
        // every chip's number, ctx, location and purpose
        let chips: Vec<(String, String, String, String)> = self
            .rows
            .iter()
            .enumerate()
            .map(|(i, r)| {
                let ctx = self.ctx_label(&r.agent).map(|c| format!(" {c}")).unwrap_or_default();
                // the number is the Alt+Shift+<n> jump key; ↗ = agent lives in another zellij session
                let remote = if !r.local && r.agent.zellij_session.is_some() { "↗" } else { "" };
                (format!(" {}{remote} ", i + 1), ctx, self.location(r), self.purpose(r))
            })
            .collect();
        // one width cap for every chip: full content when it fits, else the widest are cut; each chip costs "│", " ", " "
        let items: Vec<(usize, usize)> = chips
            .iter()
            .zip(&self.rows)
            .map(|((num, ctx, loc, purpose), r)| {
                let lo = dw(status_style(&r.agent.status, self.frame).0) + dw(num) + dw(ctx);
                (lo, (lo + dw(loc)).max(dw(purpose)))
            })
            .collect();
        let cap = shared_cap(&items, cols.saturating_sub(x + 3 * self.rows.len()), CHIP_MIN).unwrap_or(CHIP_MIN);
        for (i, (num, ctx, loc, purpose)) in chips.iter().enumerate() {
            let r = &self.rows[i];
            let (icon, _, color) = status_style(&r.agent.status, self.frame);
            let loc = trunc(loc, cap.saturating_sub(items[i].0)).trim_end().to_string();
            let status = format!("{icon}{num}{loc}{ctx}");
            let w = dw(&status).max(dw(purpose).min(cap));
            let remaining = self.rows.len() - i;
            let more = if remaining > 1 { dw(&format!(" +{}", remaining - 1)) } else { 0 };
            if x + 3 + w + more > cols {
                let tag = format!(" +{remaining}");
                l1.push_str(&format!("{DIM}{tag}{RESET}"));
                break;
            }
            // focused chip = zellij's active-tab colours; its padding spaces carry the background too
            let active = self.active_style.as_deref().unwrap_or(REV);
            // inactive chips: dark grey, so each chip reads as a tab
            let (pre, post) = if r.focused { (active, RESET) } else { (GREY_BG, RESET) };
            // waiting for you: the whole chip pulses amber (0.6s on, 0.6s off), driven by the spinner clock
            let waiting = r.agent.status == "needs_input";
            let on = (self.frame / 2) % 2 == 0;
            // unread question: the whole chip pulses; read (focused once): only the "?" blinks
            let pulse = waiting && !r.seen && on;
            let icon_hidden = waiting && r.seen && !on;
            let pre = if pulse { WAIT_BG } else { pre };
            l0.push_str(&format!("{DIM}│{RESET}{pre} "));
            l1.push_str(&format!("{DIM}│{RESET}{pre} "));
            x += 2;
            // on the coloured backgrounds (focused, pulsing) status/ctx colours turn unreadable: use the theme's
            // text colour, except the "needs you" badge and (focused only) a critical ctx, which must stand out
            let plain = !r.focused && !pulse;
            let ctx_col = match self.ctx_pct(&r.agent).map(|p| ctx_color(p as f64)) {
                Some(c) if plain || (c == RED && !pulse) => c,
                None if plain => SOFT,
                _ => "",
            };
            let (soft, branch_col) = if plain { (SOFT, CYAN) } else { ("", "") };
            let (repo, branch) = split_branch(&loc);
            let color = if pulse { "" } else if r.focused && !waiting { BOLD } else { color };
            let pad = " ".repeat(w - dw(&status));
            // blink "off": the "?" disappears, the number stays
            let icon = if icon_hidden { " " } else { icon };
            l0.push_str(&format!("{pre}{} {post}", trunc(purpose, w)));
            l1.push_str(&format!(
                "{pre}{color}{icon}{RESET}{pre}{soft}{num}{repo}{RESET}{pre}{branch_col}{branch}{RESET}{pre}{ctx_col}{ctx}{RESET}{pre}{pad} {post}"
            ));
            self.hits.push((0, x, x + w, i));
            self.hits.push((1, x, x + w, i));
            x += w + 1;
        }
        format!("{l0}\n{l1}")
    }

    /// One line per vendor with usage data: credits spent / limit, and/or the 5h, weekly and named limits. Limits
    /// that do not fit `cols` continue on further lines, indented under the first one.
    fn usage_lines(&self, cols: usize) -> Vec<Vec<(&'static str, String)>> {
        let now = self.scan.now;
        let mut lines = vec![];
        for (vendor, u) in &self.scan.usage {
            let name: String = vendor.chars().take(1).flat_map(char::to_uppercase).chain(vendor.chars().skip(1)).collect();
            let mut parts: Vec<(&'static str, String)> = vec![];
            // where each credits / limit block starts in parts: a line breaks only there
            let mut starts = vec![0];
            if let Some(e) = &u.error {
                parts.push((DIM, format!("  usage n/a ({e})")));
            } else {
                if let Some(c) = &u.credits {
                    let cur = match c.currency.as_deref().unwrap_or("USD") {
                        "USD" => "$".to_string(),
                        other => format!("{other} "),
                    };
                    parts.push(("", "  credits ".into()));
                    parts.push((MAGENTA, format!("{cur}{:.2}", c.used)));
                    parts.push(("", " / ".into()));
                    parts.push((MAGENTA, format!("{cur}{:.2}", c.limit)));
                    // the meter gets the rounded % it is printed next to, so 99.6 is a full meter beside "100%"
                    if let Some(p) = c.pct.map(f64::round) {
                        parts.push(("", " · ".into()));
                        let (lit, track) = meter(p);
                        parts.push((pct_color(p), lit));
                        parts.push((DIM, track));
                        parts.push((pct_color(p), format!(" {p:.0}%")));
                    }
                    if c.resets_at > 0 {
                        let (m, d, _) = local_date(c.resets_at, c.utc_offset_s);
                        parts.push(("", format!(" · resets {} {d}", MONTHS[m - 1])));
                    }
                }
                let fixed = [("5h", &u.five_hour), ("week", &u.seven_day)];
                let fixed = fixed.into_iter().filter_map(|(label, w)| Some((label, w.as_ref()?, false)));
                for (label, w, named) in fixed.chain(u.windows.iter().map(|w| (w.label.as_str(), w, true))) {
                    let gap = if parts.is_empty() { "  " } else { "   " };
                    starts.push(parts.len());
                    parts.push(("", format!("{gap}{label}")));
                    if let Some(p) = w.pct.map(f64::round) {
                        let (lit, track) = meter(p);
                        parts.push((pct_color(p), format!(" {lit}")));
                        parts.push((DIM, track));
                        parts.push((pct_color(p), format!(" {p:.0}%")));
                    }
                    if w.resets_at > 0 {
                        // the 5h window resets within hours: show how soon; the week: on which day; a named one
                        // like the 5h one within a day, else like the week
                        let soon = if named { w.resets_at - now < 86_400_000 } else { label == "5h" };
                        let when = if soon { ago(w.resets_at, now) } else { WEEKDAYS[local_date(w.resets_at, w.utc_offset_s).2].into() };
                        parts.push(("", format!(" · resets {when}")));
                    }
                }
            }
            if parts.is_empty() {
                continue;
            }
            let head = format!(" 📊 {name}");
            let indent = dw(&head) + 2;
            starts.push(parts.len());
            let mut w = dw(&head);
            let mut line = vec![(BOLD, head)];
            let mut on_line = 0;
            for b in starts.windows(2).filter(|b| b[0] < b[1]) {
                let mut block = parts[b[0]..b[1]].to_vec();
                let bw: usize = block.iter().map(|p| dw(&p.1)).sum();
                let gap = dw(&block[0].1) - dw(block[0].1.trim_start());
                // a block that would be cut moves to its own line, if more of it shows there
                if on_line > 0 && w + bw > cols && cols.saturating_sub(indent) > cols.saturating_sub(w) {
                    lines.push(std::mem::take(&mut line));
                    block[0].1 = format!("{}{}", " ".repeat(indent), block[0].1.trim_start());
                    (w, on_line) = (indent - gap, 0);
                }
                w += bw;
                on_line += 1;
                line.extend(block);
            }
            lines.push(line);
        }
        lines
    }

    /// Panel lines above the agent rows: counts, vendor usage lines, rule, column header.
    fn panel_header_lines(vendor_lines: usize) -> usize {
        3 + vendor_lines
    }

    fn render_panel(&mut self, rows: usize, cols: usize) -> String {
        const HEAD: [&str; 9] = ["#", "STATUS", "FOR", "TAB", "WHERE", "TASK", "MODEL", "CTX", "COST"];
        const TAB: usize = 3;
        const WHERE: usize = 4;
        const TASK: usize = 5;
        const MODEL: usize = 6;
        const CTX: usize = 7;
        const CAP: [usize; 9] = [usize::MAX, usize::MAX, usize::MAX, 12, usize::MAX, usize::MAX, 12, usize::MAX, usize::MAX];
        // below this, TASK is dropped rather than squeezed
        const TASK_MIN: usize = 10;
        // WHERE, TAB and MODEL narrow down to this before the line is cut
        const FLEX_MIN: usize = 8;
        let mut lines = vec![];
        let (needs, busy, idle, total) = self.counts();
        let now = self.scan.now;
        lines.push(fit(
            "",
            &[
                (BOLD, " 🤖 Agents".into()),
                ("", format!(" {total}   ")),
                (if needs > 0 { WAIT_FG } else { DIM }, format!("⏳ {needs} need you")),
                ("", "   ".into()),
                (YELLOW, format!("⚡ {busy} working")),
                ("", "   ".into()),
                ("\x1b[32m", format!("💤 {idle} idle")),
            ],
            cols,
        ));
        let usage = self.usage_lines(cols);
        lines.extend(usage.iter().map(|l| fit("", l, cols)));
        let rule = fit("", &[(DIM, "─".repeat(cols))], cols);
        lines.push(rule.clone());
        // every row's cells, the styles of its status and ctx cells and its lit meter cells (None: no meter), in column order
        let on = (self.frame / 2) % 2 == 0;
        let table: Vec<([String; 9], &str, &str, Option<usize>)> = self
            .rows
            .iter()
            .enumerate()
            .map(|(i, r)| {
                let a = &r.agent;
                let (icon, label, color) = status_style(&a.status, self.frame);
                let pct = self.ctx_pct(a);
                let ctx_col = pct.map_or("", |p| ctx_color(p as f64));
                let ctx = self.ctx_label(a).unwrap_or_else(|| "-".into());
                let waiting = a.status == "needs_input";
                // unread question: the status cell pulses; read: it stays amber, only the "?" blinks
                let color = if waiting && !r.seen && on {
                    WAIT_BG
                } else {
                    color
                };
                let icon = if waiting && r.seen && !on { " " } else { icon };
                let m = pct.map(|p| meter(p as f64));
                let task = if a.detail.is_empty() { self.purpose(r) } else { format!("{} · {}", self.purpose(r), a.detail) };
                let cells = [
                    format!("{}{}", if r.focused { "▶" } else { " " }, i + 1),
                    format!("{icon} {label}"),
                    ago(now, a.status_since),
                    self.tab_tag(r),
                    self.location(r),
                    task,
                    short_model(&a.model),
                    // a meter when the % is known; raw tokens (the bar's label) have none
                    match &m {
                        Some((lit, track)) => format!("{lit}{track} {ctx}"),
                        None => ctx,
                    },
                    a.cost_usd.map_or("-".into(), |c| format!("${c:.2}")),
                ];
                (cells, color, ctx_col, m.map(|(lit, _)| lit.chars().count()))
            })
            .collect();
        // one set of widths for the header and every row: widest cell, capped; WHERE and TASK share what is left
        let mut w: Vec<usize> = (0..HEAD.len())
            .map(|c| table.iter().map(|t| dw(&t.0[c])).chain([dw(HEAD[c])]).max().unwrap_or(0).min(CAP[c]))
            .collect();
        let gaps = 2 * (HEAD.len() - 1);
        let budget = cols.saturating_sub(w.iter().sum::<usize>() - w[WHERE] - w[TASK] + gaps);
        // the wider of WHERE and TASK is cut first; TASK shows when both fit with it at least TASK_MIN cells
        let cap = shared_cap(&[(0, w[WHERE]), (0, w[TASK])], budget, TASK_MIN.min(w[TASK]).max(FLEX_MIN));
        let task = cap.is_some();
        if let Some(c) = cap {
            w[WHERE] = w[WHERE].min(c);
            w[TASK] = w[TASK].min(c);
        }
        // no TASK and still too wide: narrow WHERE, then TAB, then MODEL, so CTX and COST are not cut off the right
        if !task {
            let mut over = (w.iter().sum::<usize>() - w[TASK] + gaps - 2).saturating_sub(cols);
            for c in [WHERE, TAB, MODEL] {
                let cut = over.min(w[c].saturating_sub(FLEX_MIN));
                w[c] -= cut;
                over -= cut;
            }
        }
        let shown: Vec<usize> = (0..HEAD.len()).filter(|&c| c != TASK || task).collect();
        let line = |base: &str, cells: &[String], styles: &[&str], lit: Option<usize>| {
            let pieces: Vec<(&str, String)> = shown
                .iter()
                .enumerate()
                .flat_map(|(k, &c)| {
                    let t = trunc(&cells[c], w[c]);
                    let gap = ("", if k > 0 { "  " } else { "" }.to_string());
                    // WHERE: "@branch" in cyan, split after truncation so the width stays the same
                    if c == WHERE {
                        let (repo, branch) = split_branch(&t);
                        vec![gap, (styles[c], repo.to_string()), (CYAN, branch.to_string())]
                    } else if let (CTX, Some(n)) = (c, lit) {
                        // CTX: lit meter cells and " 78%" in the ctx colour, the unlit track dim; split after truncation too
                        let (lit, rest): (String, String) = (t.chars().take(n).collect(), t.chars().skip(n).collect());
                        let (track, pct) = (rest.chars().take(5 - n).collect(), rest.chars().skip(5 - n).collect());
                        vec![gap, (styles[c], lit), (DIM, track), (styles[c], pct)]
                    } else {
                        vec![gap, (styles[c], t)]
                    }
                })
                .collect();
            fit(base, &pieces, cols)
        };
        lines.push(line("", &HEAD.map(String::from), &[BOLD; 9], None));
        let hdr = Self::panel_header_lines(usage.len());
        // agent rows win over the footer: it shows only when a content line fits above it
        let content = rows.saturating_sub(hdr);
        let footer = content > PANEL_FOOTER_LINES;
        // an error always shows: with no room for the footer it takes the last line on its own
        let error_only = !footer && content > 0 && self.error.is_some();
        let body = content - if footer { PANEL_FOOTER_LINES } else { error_only as usize };
        let start = if self.selected >= body { self.selected + 1 - body } else { 0 };
        // focused agent = zellij's active-tab colours on its "▶n" cell, like the bar's focused chip; selected row = grey
        let active = self.active_style.as_deref().unwrap_or(REV);
        for (i, (cells, color, ctx_col, lit)) in table.iter().enumerate().skip(start).take(body) {
            let mut styles = ["", "", DIM, LIGHT_GREY, "", "", LAVENDER, "", MAGENTA];
            (styles[0], styles[1], styles[7]) = (if self.rows[i].focused { active } else { "" }, color, ctx_col);
            lines.push(line(if i == self.selected { GREY_BG } else { "" }, cells, &styles, *lit));
            self.hits.push((hdr + i - start, 0, cols, i));
        }
        if self.rows.is_empty() && body > 0 {
            lines.push(fit("", &[(DIM, "  no running agents found".into())], cols));
        }
        if footer || error_only {
            if footer {
                lines.push(rule);
            }
            lines.push(match &self.error {
                Some(e) => fit("", &[(RED, format!(" {e}"))], cols),
                None => fit("", &[(DIM, " ↑↓/jk select · ⏎/click jump · n next waiting · q close".into())], cols),
            });
        }
        lines.join("\n")
    }
}

const YELLOW: &str = "\x1b[33m";
const RED: &str = "\x1b[31m";
// @branch (bar and panel WHERE), COST and credits money, panel TAB and MODEL: match the Claude status line
const CYAN: &str = "\x1b[36m";
const MAGENTA: &str = "\x1b[35m";
/// the bar's status row: number and repo, softer than the purpose above it
const SOFT: &str = "\x1b[38;5;245m";
const LIGHT_GREY: &str = "\x1b[37m";
const LAVENDER: &str = "\x1b[38;5;147m";
/// Panel lines below the agent rows: rule, help (or error).
const PANEL_FOOTER_LINES: usize = 2;
const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/// usage %: yellow from 60, red from 80
fn pct_color(p: f64) -> &'static str {
    if p >= 80.0 {
        RED
    } else if p >= 60.0 {
        YELLOW
    } else {
        ""
    }
}

/// ctx % colour (panel and bar): green up to 50, orange above, red above 80
fn ctx_color(p: f64) -> &'static str {
    if p > 80.0 {
        RED
    } else if p > 50.0 {
        "\x1b[38;5;208m"
    } else {
        "\x1b[32m"
    }
}

/// 5-cell braille meter for a % as (lit, track): each cell fills by pairs of dots, bottom row to top row, ⣀ → ⣤ → ⣶ → ⣿
/// (4 steps per cell, 20 in all, 5% each), rounded down, so only 100% is all lit.
/// The cells not reached are the track, a bare ⣀, for the caller to draw dim.
fn meter(pct: f64) -> (String, String) {
    const STEPS: [char; 4] = ['⣀', '⣤', '⣶', '⣿'];
    let n = (pct * 20.0 / 100.0).floor().clamp(0.0, 20.0) as usize;
    let lit = n.div_ceil(4);
    ((0..lit).map(|i| STEPS[(n - 4 * i).min(4) - 1]).collect(), "⣀".repeat(5 - lit))
}

/// "repo@branch" as ("repo", "@branch"); no "@" leaves it all in the first part.
fn split_branch(s: &str) -> (&str, &str) {
    s.split_at(s.find('@').unwrap_or(s.len()))
}

/// Styled pieces as exactly `w` cells: cut with "…" when too long, padded with spaces otherwise.
/// `base` (the selection colours) carries through every piece and the padding.
fn fit(base: &str, pieces: &[(&str, String)], w: usize) -> String {
    let mut o = String::new();
    let mut left = w;
    for (style, t) in pieces {
        let t = if dw(t) > left { trunc(t, left) } else { t.clone() };
        left -= dw(&t);
        o.push_str(&format!("{base}{style}{t}{RESET}"));
    }
    format!("{o}{base}{}{RESET}", " ".repeat(left))
}

/// Local calendar date of an epoch-ms time: (month 1-12, day, weekday 0 = Sunday). Civil-from-days (H. Hinnant).
fn local_date(ms: i64, utc_offset_s: i64) -> (usize, i64, usize) {
    let days = (ms.div_euclid(1000) + utc_offset_s).div_euclid(86400);
    let z = days + 719468;
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (m as usize, doy - (153 * mp + 2) / 5 + 1, (days + 4).rem_euclid(7) as usize)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// a rendered line without its ANSI colour codes
    fn strip(l: &str) -> String {
        let mut o = String::new();
        let mut esc = false;
        for c in l.chars() {
            match c {
                '\x1b' => esc = true,
                'm' if esc => esc = false,
                _ if !esc => o.push(c),
                _ => {}
            }
        }
        o
    }

    /// bar: purpose on the first row, status + repo@branch on the second, both rows equally wide
    #[test]
    fn bar_rows() {
        let mut s = State::default();
        let agent = Agent { status: "idle".into(), cwd: Some("/x/svc".into()), branch: Some("main".into()), ctx_pct: Some(78.0), title: "fix login".into(), ..Default::default() };
        s.rows = vec![Row { agent, tab_pos: None, pane_title: String::new(), local: true, focused: false, seen: false, screen_pos: (false, 0, 0) }];
        let out = s.render_bar(120);
        let lines: Vec<String> = out.split('\n').map(strip).collect();
        assert!(lines[0].contains("fix login") && lines[1].contains("svc@main 78%"), "{lines:?}");
        assert_eq!(dw(&lines[0]), dw(&lines[1]), "{lines:?}");

        // focused + seen needs_input chips, with the "?" shown and hidden: rows stay equal width, hits match the chips
        let agent = |status: &str, title: &str| Agent { status: status.into(), cwd: Some("/x/svc".into()), branch: Some("main".into()), ctx_pct: Some(90.0), title: title.into(), ..Default::default() };
        let row = |agent, focused, seen| Row { agent, tab_pos: None, pane_title: String::new(), local: true, focused, seen, screen_pos: (false, 0, 0) };
        // the display cells [a, b) of a line
        let cells = |l: &str, a: usize, b: usize| {
            let (mut x, mut out) = (0, String::new());
            for ch in l.chars() {
                let w = dw(&ch.to_string());
                if x >= a && x + w <= b {
                    out.push(ch);
                }
                x += w;
            }
            out
        };
        s.rows = vec![row(agent("idle", "first"), true, false), row(agent("needs_input", "second"), false, true), row(agent("busy", "third"), false, false)];
        for frame in [0, 2] {
            s.frame = frame;
            s.hits.clear();
            let lines: Vec<String> = s.render_bar(120).split('\n').map(strip).collect();
            assert_eq!(dw(&lines[0]), dw(&lines[1]), "frame {frame}: {lines:?}");
            for (n, title) in ["first", "second", "third"].iter().enumerate() {
                let h0 = s.hits.iter().find(|h| h.0 == 0 && h.3 == n).unwrap();
                let h1 = s.hits.iter().find(|h| h.0 == 1 && h.3 == n).unwrap();
                assert_eq!((h0.1, h0.2), (h1.1, h1.2));
                assert!(cells(&lines[0], h0.1, h0.2).trim_end() == *title, "frame {frame} chip {n}: {lines:?}");
                assert!(cells(&lines[1], h1.1, h1.2).contains(&format!(" {} svc@main", n + 1)), "frame {frame} chip {n}: {lines:?}");
            }
            // the "?" is in the second chip's range only while shown (the header's "?0" is outside every chip)
            let h = s.hits.iter().find(|h| h.0 == 1 && h.3 == 1).unwrap();
            assert_eq!(cells(&lines[1], h.1, h.2).contains('?'), frame == 0, "frame {frame}: {lines:?}");
        }

        // too narrow for every chip: "+N" appears and no row is wider than the bar
        s.frame = 0;
        for cols in [60, 40] {
            s.hits.clear();
            let lines: Vec<String> = s.render_bar(cols).split('\n').map(strip).collect();
            assert!(lines[1].contains(" +") && dw(&lines[0]) <= cols && dw(&lines[1]) <= cols, "cols {cols}: {lines:?}");
        }

        // room to spare: a title and repo@branch longer than 36 cells both show in full
        let long = |title: &str| Agent { status: "idle".into(), cwd: Some("/x/eng-intelligence".into()), branch: Some("feat/168-long-branch-name".into()), ctx_pct: Some(40.0), title: title.into(), ..Default::default() };
        let title = "refactor the scanner so sessions survive restarts";
        s.rows = vec![row(long(title), false, false)];
        let lines: Vec<String> = s.render_bar(200).split('\n').map(strip).collect();
        assert!(lines[0].contains(title) && lines[1].contains("eng-intelligence@feat/168-long-branch-name 40%"), "{lines:?}");
        assert!(!lines.iter().any(|l| l.contains('…')), "{lines:?}");

        // short of room: only the long chip is cut, the short one stays whole, and no "+N"
        s.rows = vec![row(long(title), false, false), row(agent("idle", "fix"), false, false)];
        let cols = 80;
        let lines: Vec<String> = s.render_bar(cols).split('\n').map(strip).collect();
        assert!(lines[0].contains("refactor the scanner") && lines[0].contains('…') && lines[1].contains("eng-intelligence@"), "{lines:?}");
        assert!(lines[0].contains(" fix ") && lines[1].contains(" 2 svc@main 90%") && !lines[1].contains(" +"), "{lines:?}");
        assert!(dw(&lines[0]) == dw(&lines[1]) && dw(&lines[0]) <= cols, "{lines:?}");
    }

    #[test]
    fn shared_cap_cuts_widest_first() {
        // everything fits: the widest natural width
        assert_eq!(shared_cap(&[(0, 10), (0, 30)], 40, 5), Some(30));
        // only the widest is cut; the short item keeps its 10
        assert_eq!(shared_cap(&[(0, 10), (0, 30)], 30, 5), Some(20));
        // two wide items share one cap; `lo` never shrinks
        assert_eq!(shared_cap(&[(0, 10), (0, 30), (0, 40)], 40, 5), Some(15));
        assert_eq!(shared_cap(&[(12, 14), (0, 30)], 20, 5), Some(8));
        // below the floor: None
        assert_eq!(shared_cap(&[(0, 10), (0, 30)], 14, 8), None);
        assert_eq!(shared_cap(&[(0, 10), (0, 30)], 16, 8), Some(8));
        // the floor is FLEX_MIN even when TASK is only 4 cells: 42-cell WHERE is not cut to 5 for it
        assert_eq!(shared_cap(&[(0, 42), (0, 4)], 9, 4.max(8)), None);
        assert_eq!(shared_cap(&[(0, 5), (0, 4)], 9, 4.max(8)), Some(5));
    }

    #[test]
    fn meter_fills_cells_by_dot_pairs() {
        let m = |p: f64| {
            let (lit, track) = meter(p);
            assert_eq!(lit.chars().count() + track.chars().count(), 5, "{p}%");
            (lit, track)
        };
        let s = |a: &str, b: &str| (a.to_string(), b.to_string());
        assert_eq!(m(0.0), s("", "⣀⣀⣀⣀⣀"));
        assert_eq!(m(5.0), s("⣀", "⣀⣀⣀⣀"));
        assert_eq!(m(10.0), s("⣤", "⣀⣀⣀⣀"));
        assert_eq!(m(20.0), s("⣿", "⣀⣀⣀⣀"));
        assert_eq!(m(50.0), s("⣿⣿⣤", "⣀⣀"));
        assert_eq!(m(99.0), s("⣿⣿⣿⣿⣶", ""));
        assert_eq!(m(100.0), s("⣿⣿⣿⣿⣿", ""));
        assert_eq!(m(140.0), s("⣿⣿⣿⣿⣿", ""));
        assert_eq!(m(-5.0), s("", "⣀⣀⣀⣀⣀"));
        assert_eq!(m(f64::NAN), s("", "⣀⣀⣀⣀⣀"));
    }

    #[test]
    fn split_branch_after_trunc() {
        assert_eq!(split_branch("repo@branch"), ("repo", "@branch"));
        assert_eq!(split_branch("repo"), ("repo", ""));
        assert_eq!(split_branch(&trunc("repo@branch", 8)), ("repo", "@br…"));
    }

    /// every panel line is exactly `cols` cells wide, at a wide and a narrow width
    #[test]
    fn panel_lines_fill_width() {
        let mut s = State::default();
        s.scan = serde_json::from_str(r#"{"now": 100000, "agents": [], "usage": {"claude": {
            "five_hour": {"pct": 48, "resets_at": 200000}, "seven_day": {"pct": 23}}}}"#)
        .unwrap();
        let agent = |status: &str, ctx_pct, branch: &str| Agent {
            status: status.into(),
            cwd: Some("/x/svc".into()),
            branch: Some(branch.into()),
            ctx_pct,
            ctx_tokens: Some(156000),
            title: "fix login".into(),
            cost_usd: Some(1.2),
            ..Default::default()
        };
        let row = |agent| Row { agent, tab_pos: None, pane_title: String::new(), local: true, focused: false, seen: false, screen_pos: (false, 0, 0) };
        s.rows = vec![row(agent("needs_input", Some(78.0), "main")), row(agent("busy", None, "feat/5")), row(agent("idle", Some(5.0), "x"))];
        for cols in [120, 40, 8] {
            let out = s.render_panel(20, cols);
            let lines: Vec<&str> = out.split('\n').collect();
            // counts, usage (the week limit on its own line at 40 columns), rule, column header, 3 agents, rule, help
            let usage = if cols == 40 { 2 } else { 1 };
            assert_eq!(lines.len(), State::panel_header_lines(usage) + 3 + PANEL_FOOTER_LINES);
            for l in lines {
                assert_eq!(dw(&strip(l)), cols, "{cols}: {:?}", strip(l));
            }
        }
        // 78%: lit cells in the ctx colour (orange), the unreached track dim
        let out = s.render_panel(20, 120);
        assert!(out.contains("\x1b[38;5;208m⣿⣿⣿⣶") && out.contains("\x1b[2m⣀"), "{out:?}");
        // long WHERE and TAB narrow rather than push CTX and COST off the right
        for r in &mut s.rows {
            (r.agent.cwd, r.agent.branch) = (Some("/x/eng-intelligence".into()), Some("feat/168-long-branch-name".into()));
            (r.agent.model, r.agent.cost_usd, r.local, r.agent.zellij_session) = (Some("claude-sonnet-5-5".into()), Some(12.34), false, Some("work-session-long".into()));
        }
        // at 70 cols WHERE, TAB and MODEL all narrow (TASK dropped), and CTX and COST still show
        for cols in [70, 80, 100] {
            let out = s.render_panel(20, cols);
            let lines: Vec<String> = out.split('\n').map(strip).collect();
            let head = &lines[State::panel_header_lines(1) - 1];
            assert!(head.contains("CTX") && head.contains("COST"), "{cols}: {head:?}");
            let first = &lines[State::panel_header_lines(1)];
            assert!(first.contains("$12.34") && first.contains("78%"), "{cols}: {first:?}");
            assert!(lines.iter().all(|l| dw(l) == cols), "{cols}: {lines:?}");
        }
        // room to spare: a WHERE over 28 cells and a long TASK both show in full
        s.rows[0].agent.title = "refactor the scanner so sessions survive restarts".into();
        let lines: Vec<String> = s.render_panel(20, 200).split('\n').map(strip).collect();
        let first = &lines[State::panel_header_lines(1)];
        assert!(first.contains("eng-intelligence@feat/168-long-branch-name") && first.contains("survive restarts"), "{first:?}");
        // short of room: the shorter of WHERE and TASK stays whole, the longer is cut
        let lines: Vec<String> = s.render_panel(20, 157).split('\n').map(strip).collect();
        let first = &lines[State::panel_header_lines(1)];
        assert!(first.contains("eng-intelligence@feat/168-long-branch-name") && first.contains("refactor the scanner") && first.contains('…'), "{first:?}");
        assert!(!first.contains("survive restarts") && lines.iter().all(|l| dw(l) == 157), "{lines:?}");
        s.rows[0].agent.title = "fix login".into();
        // every TASK shorter than FLEX_MIN: TASK is dropped rather than WHERE cut below FLEX_MIN, lines stay `cols` wide
        for r in &mut s.rows {
            r.agent.title = "fix".into();
        }
        for cols in 60..=100 {
            let lines: Vec<String> = s.render_panel(20, cols).split('\n').map(strip).collect();
            assert!(lines.iter().all(|l| dw(l) == cols), "{cols}: {lines:?}");
        }
        s.rows[0].agent.title = "fix login".into();
        // a short TASK leaves the spare width at the end, not between TASK and MODEL
        let out = s.render_panel(20, 200);
        let head = strip(out.split('\n').nth(State::panel_header_lines(1) - 1).unwrap());
        assert!(dw(&head[..head.find("CTX").unwrap()]) < 200 - 20 && dw(&head) == 200, "{head:?}");
        // a short pane keeps an agent row over the footer and never overflows
        let hdr = State::panel_header_lines(1);
        for rows in [hdr + 1, hdr + 2, hdr + 3] {
            s.hits.clear();
            let out = s.render_panel(rows, 120);
            assert!(out.split('\n').count() <= rows);
            assert!(!s.hits.is_empty());
            assert!(s.hits.iter().all(|h| h.0 < rows));
        }
        // an error still shows on the last line when the footer does not fit
        s.error = Some("scan failed".into());
        for rows in [hdr + 1, hdr + 2] {
            s.hits.clear();
            let out = s.render_panel(rows, 120);
            let lines: Vec<&str> = out.split('\n').collect();
            assert!(lines.len() <= rows && strip(lines.last().unwrap()).contains("scan failed"), "{rows}: {lines:?}");
            assert!(s.hits.iter().all(|h| h.0 < rows - 1));
        }
        assert_eq!(s.hits.len(), 1);
    }

    /// a vendor with only named windows (agy's quota buckets) gets one labelled meter each, and the panel still fits
    #[test]
    fn panel_named_usage_windows() {
        let mut s = State::default();
        s.scan = serde_json::from_str(r#"{"now": 100000, "agents": [], "usage": {"agy": {"windows": [
            {"label": "gemini 5h", "pct": 12, "resets_at": 3700000},
            {"label": "3p week", "pct": 100, "resets_at": 300000000}]}}}"#)
        .unwrap();
        let line = strip(&s.render_panel(20, 200).split('\n').nth(1).unwrap().to_string());
        // within a day: how soon, like the 5h window; further off: the weekday, like the week (epoch day 3 = Sunday)
        assert!(line.starts_with(" 📊 Agy  gemini 5h ⣤⣀⣀⣀⣀ 12% · resets 1h00m   3p week ⣿⣿⣿⣿⣿ 100% · resets Sun"), "{line:?}");
        for cols in [120, 40, 8] {
            for l in s.render_panel(20, cols).split('\n') {
                assert_eq!(dw(&strip(l)), cols, "{cols}: {:?}", strip(l));
            }
        }
        // all five agy buckets: what does not fit 120 columns continues below, under the first meter
        s.scan = serde_json::from_str(r#"{"now": 100000, "agents": [], "usage": {"agy": {"windows": [
            {"label": "gemini 5h", "pct": 12, "resets_at": 3700000},
            {"label": "gemini week", "pct": 40, "resets_at": 300000000},
            {"label": "argon week", "pct": 65, "resets_at": 300000000},
            {"label": "3p 5h", "pct": 85, "resets_at": 3700000},
            {"label": "3p week", "pct": 100, "resets_at": 300000000}]}}}"#)
        .unwrap();
        let labels = ["gemini 5h", "gemini week", "argon week", "3p 5h", "3p week"];
        // narrower: one limit per line, cut at its end rather than dropped
        for (cols, n) in [(120, 2), (40, 5)] {
            let lines: Vec<String> = s.render_panel(20, cols).split('\n').map(strip).collect();
            let usage = &lines[1..State::panel_header_lines(s.usage_lines(cols).len()) - 2];
            assert_eq!(usage.len(), n, "{usage:?}");
            for l in labels {
                assert!(usage.iter().any(|u| u.contains(&format!("{l} ⣿")) || u.contains(&format!("{l} ⣤"))), "{l}: {usage:?}");
            }
            let col = dw(" 📊 Agy  ");
            for u in &usage[1..] {
                assert!(u.chars().take(col).all(|c| c == ' ') && !u.chars().nth(col).unwrap().is_whitespace(), "{u:?}");
            }
        }
        // wide enough: one line, as before
        assert_eq!(s.usage_lines(250).len(), 1);
        for cols in [120, 40, 8] {
            let out = s.render_panel(20, cols);
            assert_eq!(out.split('\n').nth(State::panel_header_lines(s.usage_lines(cols).len()) - 2).map(strip), Some("─".repeat(cols)));
            for l in out.split('\n') {
                assert_eq!(dw(&strip(l)), cols, "{cols}: {:?}", strip(l));
            }
        }
    }

    /// visibility from Event::ListClients: a client focused on our own pane is left out of the list
    #[test]
    fn hidden_from_clients() {
        let mut s = State::default();
        s.my_id = 7;
        let pane = |id, is_plugin| PaneInfo { id, is_plugin, ..Default::default() };
        s.panes.panes.insert(0, vec![pane(7, true), pane(1, false)]);
        s.panes.panes.insert(1, vec![pane(2, false)]);
        let client = |pane_id, current| ClientInfo::new(1, pane_id, String::new(), current);
        s.my_tab = Some(0);
        s.mode = Mode::Panel;
        // the panel holds focus: the current client is missing
        assert!(!s.hidden_from(&[client(PaneId::Terminal(2), false)]));
        assert!(!s.hidden_from(&[]));
        // the current client on our own pane (listed)
        assert!(!s.hidden_from(&[client(PaneId::Plugin(7), true)]));
        // the current client on a terminal in our tab
        assert!(!s.hidden_from(&[client(PaneId::Terminal(1), true)]));
        // the current client on a terminal in another tab
        assert!(s.hidden_from(&[client(PaneId::Terminal(2), true)]));
        // a bar is never focused: a missing current client keeps the old rule
        s.mode = Mode::Bar;
        assert!(s.hidden_from(&[client(PaneId::Terminal(2), false)]));
        assert!(s.hidden_from(&[]));
        assert!(!s.hidden_from(&[client(PaneId::Terminal(1), true)]));
        assert!(s.hidden_from(&[client(PaneId::Terminal(2), true)]));
        s.my_tab = None;
        assert!(!s.hidden_from(&[client(PaneId::Terminal(2), true)]));
    }
}
