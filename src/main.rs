use serde::Deserialize;
use std::collections::{BTreeMap, HashMap};
use zellij_tile::prelude::*;

// ---------- data from the scanner ----------

#[derive(Deserialize, Default, Clone)]
struct Scan {
    now: i64,
    agents: Vec<Agent>,
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
    /// (floating, y, x) on screen: numbers go top-to-bottom, left-to-right within a tab
    screen_pos: (bool, usize, usize),
}

struct State {
    mode: Mode,
    scanner: String,
    interval: f64,
    hide_on_jump: bool,
    debug_dump: Option<String>,
    session: Option<String>,
    tabs: Vec<TabInfo>,
    panes: PaneManifest,
    scan: Scan,
    rows: Vec<Row>,
    selected: usize,
    // clickable regions from the last render: (line, col_start, col_end, row index)
    hits: Vec<(usize, usize, usize, usize)>,
    permitted: bool,
    scanning: bool,
    my_id: u32,
    my_tab: Option<usize>,
    ticks: u64,
    last_scan_tick: u64,
    hidden: bool,
    /// terminal panes the attached clients are focused on (authoritative; PaneInfo.is_focused goes stale)
    client_focus: Vec<u32>,
    error: Option<String>,
}

impl Default for State {
    fn default() -> Self {
        Self {
            mode: Mode::Bar,
            scanner: "zam-scan.py".into(),
            interval: 2.0,
            hide_on_jump: true,
            debug_dump: None,
            session: None,
            tabs: vec![],
            panes: PaneManifest::default(),
            scan: Scan::default(),
            rows: vec![],
            selected: 0,
            hits: vec![],
            permitted: false,
            scanning: false,
            my_id: 0,
            my_tab: None,
            ticks: 1,
            last_scan_tick: 0,
            hidden: false,
            client_focus: Vec::new(),
            error: None,
        }
    }
}

register_plugin!(State);

impl ZellijPlugin for State {
    fn load(&mut self, cfg: BTreeMap<String, String>) {
        if cfg.get("mode").map(|m| m == "panel").unwrap_or(false) {
            self.mode = Mode::Panel;
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
        self.my_id = get_plugin_ids().plugin_id;
        request_permission(&[
            PermissionType::ReadApplicationState,
            PermissionType::ChangeApplicationState,
            PermissionType::RunCommands,
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
                set_timeout(self.interval);
                true
            }
            Event::PermissionRequestResult(PermissionStatus::Denied) => {
                self.error = Some("permissions denied".into());
                true
            }
            Event::ModeUpdate(mi) => {
                let changed = self.session != mi.session_name;
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
                self.my_tab = pm
                    .panes
                    .iter()
                    .find(|(_, ps)| ps.iter().any(|p| p.is_plugin && p.id == self.my_id))
                    .map(|(t, _)| *t);
                self.panes = pm;
                self.rebuild();
                list_clients();
                true
            }
            Event::Timer(_) => {
                // Visibility check first; the scan happens in ListClients if we are on screen.
                self.ticks += 1;
                list_clients();
                set_timeout(self.interval);
                false
            }
            Event::ListClients(clients) => {
                // Only the instance in a tab some client is looking at scans; hidden tabs idle.
                // (TabInfo.active goes stale for plugins in hidden tabs, and hidden plugins
                // still render, so neither can tell visibility.)
                let was_hidden = self.hidden;
                self.hidden = match self.my_tab {
                    Some(mine) => !clients.iter().any(|c| self.tab_of(&c.pane_id) == Some(mine)),
                    None => false,
                };
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
                if focus != self.client_focus {
                    self.client_focus = focus;
                    self.rebuild();
                    return true;
                }
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
                    hide_self();
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
        if msg.name != "zam_jump" || self.hidden || self.my_tab.is_none() || !self.permitted {
            return false;
        }
        match msg.payload.as_deref().map(str::trim) {
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
        false
    }

    fn render(&mut self, rows: usize, cols: usize) {
        self.hits.clear();
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
        let mut ctx = BTreeMap::new();
        ctx.insert("zam".to_string(), "scan".to_string());
        // trailing args are ignored by the scanner; they show up in `ps` and help debugging
        let who = format!("id={}", self.my_id);
        // run_command has no shell, so expand a leading "~/" here (via sh, which knows $HOME)
        if let Some(rest) = self.scanner.strip_prefix("~/") {
            let script = r#"exec python3 -I "$HOME/$1" --from "$2""#;
            run_command(&["sh", "-c", script, "sh", rest, &who], ctx);
        } else {
            run_command(&["python3", "-I", &self.scanner, "--from", &who], ctx);
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
        self.rows = rows;
        if self.selected >= self.rows.len() {
            self.selected = self.rows.len().saturating_sub(1);
        }
    }

    fn move_sel(&mut self, d: isize) -> bool {
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
        let Some(row) = self.rows.get(idx) else { return };
        let Some(pane) = row.agent.pane_id else { return };
        if row.local {
            focus_terminal_pane(pane, false, false);
        } else if let Some(s) = &row.agent.zellij_session {
            switch_session_with_focus(s, None, Some((pane, false)));
        }
        if self.mode == Mode::Panel && self.hide_on_jump {
            hide_self();
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

fn status_style(status: &str) -> (&'static str, &'static str, &'static str) {
    // (icon, label, colour)
    match status {
        "needs_input" => ("!", "NEEDS YOU", "\x1b[1;37;41m"),
        "busy" => ("●", "working", "\x1b[33m"),
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

fn trunc(s: &str, w: usize) -> String {
    let n = s.chars().count();
    if n <= w {
        format!("{s:<w$}")
    } else if w == 0 {
        String::new()
    } else {
        let mut t: String = s.chars().take(w - 1).collect();
        t.push('…');
        t
    }
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

    fn tab_tag(&self, r: &Row) -> String {
        match (r.tab_pos, &r.agent.zellij_session) {
            (Some(p), _) => format!("{}", p + 1),
            (None, Some(s)) if !r.local => format!("↗{s}"),
            _ => "?".into(),
        }
    }

    /// Two-row bar. Row 0: status icon, tab, location, ctx. Row 1: purpose. Chips split by │.
    fn render_bar(&mut self, cols: usize) -> String {
        const CHIP_MAX: usize = 36;
        let (needs, busy, idle, total) = self.counts();
        let mut l0 = String::new();
        let mut l1 = String::new();
        let mut x = 0usize;
        // header block
        let h0 = format!(" AGENTS {total} ");
        let h1 = format!(" !{needs} ●{busy} ○{idle} ");
        let hw = h0.chars().count().max(h1.chars().count());
        l0.push_str(&format!("{BOLD}{}{RESET}", trunc(&h0, hw)));
        let needs_style = if needs > 0 { "\x1b[1;31m" } else { DIM };
        l1.push_str(&format!("{DIM} {needs_style}!{needs}{RESET}{DIM} \x1b[33m●{busy}{RESET}{DIM} \x1b[32m○{idle}{RESET}{}", " ".repeat(hw - h1.chars().count() + 1)));
        x += hw;
        if let Some(e) = &self.error {
            let e = trunc(e, cols.saturating_sub(x + 3).min(60));
            let w = e.chars().count();
            l0.push_str(&format!("{DIM}│{RESET} \x1b[31m{e}{RESET} "));
            l1.push_str(&format!("{DIM}│{RESET} {} ", " ".repeat(w)));
            x += w + 3;
        }
        for i in 0..self.rows.len() {
            let r = &self.rows[i];
            let (icon, _, color) = status_style(&r.agent.status);
            let ctx = self.ctx_label(&r.agent).map(|c| format!(" {c}")).unwrap_or_default();
            // the number is the Alt+Shift+<n> jump key; ↗ = agent lives in another zellij session
            let remote = if !r.local && r.agent.zellij_session.is_some() { "↗" } else { "" };
            let head = format!("{icon} {}{remote} ", i + 1);
            let loc_w = CHIP_MAX.saturating_sub(head.chars().count() + ctx.chars().count());
            let loc = trunc(&self.location(r), loc_w).trim_end().to_string();
            let top = format!("{head}{loc}{ctx}");
            let purpose = self.purpose(r);
            let w = top.chars().count().max(purpose.chars().count().min(CHIP_MAX));
            let remaining = self.rows.len() - i;
            let more = if remaining > 1 { format!(" +{}", remaining - 1).chars().count() } else { 0 };
            if x + 3 + w + more > cols {
                let tag = format!(" +{remaining}");
                l0.push_str(&format!("{DIM}{tag}{RESET}"));
                break;
            }
            let (pre, post) = if r.focused { (REV, RESET) } else { ("", "") };
            l0.push_str(&format!("{DIM} │ {RESET}"));
            l1.push_str(&format!("{DIM} │ {RESET}"));
            x += 3;
            let ctx_col = match self.ctx_pct(&r.agent) {
                Some(p) if p >= 80 => "\x1b[31m",
                Some(p) if p >= 60 => "\x1b[33m",
                _ => DIM,
            };
            let pad0 = " ".repeat(w - top.chars().count());
            // SGR 5 = blink: the terminal animates it, no redraw timer needed
            let blink = if r.agent.status == "needs_input" { "\x1b[5m" } else { "" };
            l0.push_str(&format!("{pre}{color}{blink}{head}{RESET}{pre}{BOLD}{loc}{RESET}{pre}{ctx_col}{ctx}{RESET}{pre}{pad0}{post}"));
            l1.push_str(&format!("{pre}{}{post}", trunc(&purpose, w)));
            self.hits.push((0, x, x + w, i));
            self.hits.push((1, x, x + w, i));
            x += w;
        }
        format!("{l0}\n{l1}")
    }

    fn render_panel(&mut self, rows: usize, cols: usize) -> String {
        use std::fmt::Write;
        let mut o = String::new();
        let (needs, busy, idle, total) = self.counts();
        let now = self.scan.now;
        let _ = writeln!(o, 
            "{BOLD} Agents{RESET} {total}  {}{needs} need you{RESET}  \x1b[33m{busy} working{RESET}  \x1b[32m{idle} idle{RESET}",
            if needs > 0 { "\x1b[1;31m" } else { DIM }
        );
        if let Some(e) = &self.error {
            let _ = writeln!(o, "\x1b[31m {}{RESET}", trunc(e, cols.saturating_sub(1)));
        } else {
            let _ = writeln!(o, "{DIM} ↑↓/jk select · enter/click jump · n next waiting · q hide{RESET}");
        }
        // columns: status(11) tab(10) location(34) since(7) ctx(6) model(13) up(7) task(rest)
        let fixed = 2 + 11 + 10 + 34 + 7 + 6 + 13 + 7;
        let detail_w = cols.saturating_sub(fixed);
        let _ = writeln!(o, 
            "{DIM}    {:<11}{:<10}{:<34}{:<7}{:<6}{:<13}{:<7}{}{RESET}",
            "STATUS", "TAB", "LOCATION", "SINCE", "CTX", "MODEL", "UP", trunc("TASK", detail_w)
        );
        let body = rows.saturating_sub(3);
        let start = if self.selected >= body { self.selected + 1 - body } else { 0 };
        for (i, r) in self.rows.iter().enumerate().skip(start).take(body) {
            let a = &r.agent;
            let (icon, label, color) = status_style(&a.status);
            let tab = self.tab_tag(r);
            let loc = self.location(r);
            let ctx = self.ctx_label(a).unwrap_or("-".into());
            let ctx_col = match self.ctx_pct(a) {
                Some(p) if p >= 80 => "\x1b[31m",
                Some(p) if p >= 60 => "\x1b[33m",
                _ => "",
            };
            let detail = if a.detail.is_empty() {
                self.purpose(r)
            } else {
                format!("{} · {}", self.purpose(r), a.detail)
            };
            let sel = i == self.selected;
            let (pre, post) = if sel { (REV, RESET) } else { ("", "") };
            let blink = if a.status == "needs_input" { "\x1b[5m" } else { "" };
            let marker = if r.focused { format!("▶{:<2}", i + 1) } else { format!(" {:<2}", i + 1) };
            let _ = writeln!(o, 
                "{pre}{marker} {color}{blink}{icon} {}{RESET}{pre}{}{}{}{ctx_col}{}{RESET}{pre}{}{}{}{post}",
                trunc(label, 9),
                trunc(&tab, 9) + " ",
                trunc(&loc, 33) + " ",
                trunc(&ago(now, a.status_since), 7),
                trunc(&ctx, 6),
                trunc(&short_model(&a.model), 12) + " ",
                trunc(&ago(now, a.started_at), 7),
                trunc(&detail, detail_w),
            );
            self.hits.push((3 + i - start, 0, cols, i));
        }
        if self.rows.is_empty() {
            let _ = writeln!(o, "{DIM}  no running agents found{RESET}");
        }
        o
    }
}
