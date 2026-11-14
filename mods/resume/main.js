// Resume Claude Code sessions after a reboot. One mod, two roles:
//   no overlay  the watcher: a floating block that draws nothing. When it starts and whenever the terminals change,
//               it asks hook.py which sessions can be resumed and posts one notification for each new set.
//   overlay     the dialog (`mod.open {name: "resume"}`, also the notification's action): the sessions with a
//               checkbox each, Resume selected and Dismiss, and the Claude Code hook setup per config dir.
// hook.py (next to this file) is the Claude Code hook that records which session runs in which terminal, and the
// helper this mod runs for everything else. Nothing resumes by itself: every resume reloads a conversation and
// costs tokens, so only the Resume button sends the command.
import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec, hc, appHome, home, notify, props, closeOverlay, shared, setShared, terminals, theme } from "herdr";

const KEY = "resume.notified"; // the candidate set the last notification was about
let hinted = false; // the "set it up" notification was shown in this app run (module scope: shared by every copy)
const OPEN = { name: "mod.open", args: { name: "resume" } };

async function helper(...args) {
  const r = await exec("python3", [`${appHome()}/mods/resume/hook.py`, ...args]);
  if (r.code !== 0) throw new Error((r.stderr || "hook.py failed").trim().split("\n").pop());
  return JSON.parse(r.stdout);
}

const ago = (s) => {
  const d = Math.max(0, Date.now() / 1000 - s);
  return d < 60 ? "now" : d < 3600 ? `${Math.floor(d / 60)} min` : d < 86400 ? `${Math.floor(d / 3600)} h` : `${Math.floor(d / 86400)} d`;
};
const kilo = (n) => (n < 1000 ? String(n) : n < 1e6 ? `${Math.round(n / 1000)}K` : `${(n / 1e6).toFixed(1)}M`);
const usd = (c) => (c < 0.01 ? "<$0.01" : `$${c.toFixed(2)}`);
// Each row's r.cache comes from `hook.py candidates --cost`: what resuming writes to the prompt cache.
const CACHE_TIP = "The prompt cache expired with the restart. The first message after resuming writes the whole context "
  + "to the cache once; later turns read it at a tenth of the price. Cost at Anthropic API list prices; a subscription "
  + "isn't billed per token.";
const tilde = (p, h) => (p === h || p.startsWith(`${h}/`) ? `~${p.slice(h.length)}` : p);

export default class Resume extends View {
  init(_props, cx) {
    this.overlay = props()?.overlay ?? null;
    this.busy = false;
    if (!this.overlay) return this.hint(cx);
    this.rows = null; // null: loading
    this.dirs = [];
    this.off = new Set(); // unchecked terminals
    this.err = null;
    this.load(cx);
  }

  // Once per app run, for as long as no config dir is set up: without the hooks nothing is recorded, and after a
  // restart there would be nothing to offer and nothing saying why.
  hint(cx) {
    if (hinted) return;
    hinted = true;
    cx.spawn(async () => {
      try {
        const { dirs } = await helper("status");
        if (dirs.some((d) => d.installed)) return;
        notify({ title: "Resume Claude sessions after a restart", body: "Click to set up the Claude Code hooks it needs.", level: "info", action: OPEN });
      } catch {}
    });
  }

  // The watcher: runs on start and when the terminals change (render runs then), never on a timer.
  watch(cx) {
    const sig = JSON.stringify(terminals().map((t) => [t.address, t.agent, t.state]));
    if (sig === this.sig) return;
    this.sig = sig;
    if (this.busy) { this.again = true; return; }
    this.busy = true;
    cx.spawn(async (cx) => {
      try {
        const { candidates } = await helper("candidates");
        const keys = candidates.map((c) => `${c.terminal}:${c.session_id}`).sort();
        const before = String(shared(KEY) ?? "");
        if (keys.join(",") !== before) {
          setShared(KEY, keys.join(","));
          // Only a session not in the last notification is news (resuming some of them shrinks the set).
          const seen = new Set(before.split(","));
          const n = candidates.length;
          if (keys.some((k) => !seen.has(k))) notify({ title: `${n} Claude session${n === 1 ? "" : "s"} can be resumed`, body: "Click to choose which ones.", level: "info", action: OPEN });
        }
      } catch {}
      this.busy = false;
      if (this.again) { this.again = false; this.sig = null; this.watch(cx); }
    });
  }

  load(cx) {
    cx.spawn(async (cx) => {
      try {
        this.rows = (await helper("candidates", "--cost")).candidates; // once per dialog: reads transcript tails
        const status = await helper("status");
        this.dirs = status.dirs;
        this.home = status.home; // hook.py's home: RESUME_FIXTURE's in the demo, else the same as home()
        this.err = null;
      } catch (e) { this.rows = this.rows ?? []; this.err = String(e?.message ?? e); }
      cx.notify();
    });
  }

  act(cx, fn) {
    if (this.busy) return;
    this.busy = true;
    cx.notify();
    cx.spawn(async (cx) => {
      try { await fn(); this.err = null; } catch (e) { this.err = String(e?.message ?? e); }
      this.busy = false;
      cx.notify();
    });
  }

  resume(cx) {
    const picked = this.rows.filter((r) => !this.off.has(r.terminal));
    this.act(cx, async () => {
      const failed = [];
      for (const r of picked) {
        const res = await hc(["send", r.terminal, r.command]).catch((e) => ({ code: 1, stderr: String(e) }));
        if (res.code !== 0) failed.push(`${r.terminal}: ${(res.stderr || "send failed").trim()}`);
      }
      if (failed.length) throw new Error(failed.join("; "));
      closeOverlay(this.overlay);
    });
  }

  dismiss(cx) {
    this.act(cx, async () => {
      await helper("dismiss", ...this.rows.map((r) => r.terminal));
      setShared(KEY, "");
      closeOverlay(this.overlay);
    });
  }

  setup(cx, what) {
    this.act(cx, async () => { this.dirs = (await helper(what)).dirs; });
  }

  render(cx) {
    if (!this.overlay) { this.watch(cx); return div(); }
    const t = theme();
    const h = this.home ?? home();
    const button = (id, label, primary, enabled, onClick) => div().id(id).px(10).py(4).rounded(5).text_size(12)
      .bg(primary ? t.solid : t.raised).text_color(primary ? t.solid_foreground : t.foreground)
      .when(!enabled, (el) => el.opacity(0.45))
      .when(enabled, (el) => el.cursor_pointer().hover((s) => s.bg(primary ? t.accent : t.hover)).on_click(onClick))
      .child(label);
    const rows = this.rows ?? [];
    const ticked = rows.filter((r) => !this.off.has(r.terminal)), picked = ticked.length;
    const est = ticked.map((r) => r.cache).filter(Boolean);
    const priced = est.filter((c) => c.cost != null);
    const total = est.length === 0 ? null : `≈ ${kilo(est.reduce((a, c) => a + c.tokens, 0))} tokens to cache`
      + (priced.length ? ` · ~${usd(priced.reduce((a, c) => a + c.cost, 0))}${priced.length < est.length ? "+" : ""} at API prices` : "");

    const row = (r) => {
      const on = !this.off.has(r.terminal);
      return h_flex().id(`row-${r.terminal}`).gap(10).px(8).py(6).rounded(5).items_center().cursor_pointer()
        .hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => { on ? this.off.add(r.terminal) : this.off.delete(r.terminal); cx.notify(); })
        .child(h_flex().size(16).flex_none().rounded(4).border_1().border_color(on ? t.accent : t.input)
          .bg(on ? t.accent : t.background).text_color(t.accent_foreground).text_size(11)
          .items_center().justify_center().child(on ? "✓" : ""))
        .child(v_flex().flex_1().min_w(0)
          .child(div().text_size(13).text_color(t.foreground).truncate().child(r.where))
          .child(div().text_size(11.5).text_color(t.dim).truncate().child(tilde(r.cwd, h))))
        .child(div().flex_none().px(6).rounded(4).bg(t.raised).text_size(11).text_color(t.muted).child(r.label))
        .child(div().flex_none().w(110).text_size(11.5).text_color(t.dim).truncate()
          .when(Boolean(r.cache), (el) => el.tooltip(`${r.cache.model ?? "Unknown model"}, ${r.cache.ttl} cache. ${CACHE_TIP}`)
            .child(`${kilo(r.cache.tokens)} tok${r.cache.cost != null ? ` · ~${usd(r.cache.cost)}` : ""}`)))
        .child(div().flex_none().w(44).text_size(11.5).text_color(t.dim).child(ago(r.last ?? r.at)));
    };

    const anyOn = this.dirs.some((d) => d.installed), anyOff = this.dirs.some((d) => !d.installed);
    const hooks = v_flex().gap(6).pt(10).border_t_1().border_color(t.border)
      .child(h_flex().justify_between().items_center()
        .child(div().text_size(12).font_semibold().text_color(t.foreground).child("Claude Code hooks"))
        .child(h_flex().gap(6)
          .when(anyOn, (el) => el.child(button("remove", "Remove", false, !this.busy, (_e, cx) => this.setup(cx, "remove"))))
          .when(anyOff, (el) => el.child(button("setup", "Set up", true, !this.busy, (_e, cx) => this.setup(cx, "setup"))))))
      .child(div().text_size(11.5).text_color(t.dim)
        .child("Set up adds a SessionStart and a SessionEnd hook to each config dir's settings.json. Your other hooks stay."))
      .children(this.dirs.map((d) => h_flex().gap(8).text_size(12)
        .child(div().w(8).h(8).rounded_full().bg(d.installed ? t.ansi2 : t.dim))
        .child(div().flex_1().truncate().text_color(t.foreground).child(tilde(d.dir, h)))
        .child(div().text_color(d.error ? t.blocked : t.muted).child(d.error ?? (d.installed ? "set up" : "not set up")))))
      .when(this.dirs.length === 0 && this.rows !== null, (el) => el.child(div().text_size(12).text_color(t.muted).child("No Claude config dir found (~/.claude).")));

    return v_flex().id("root").size_full().bg(t.popover).p(14).gap(10).text_color(t.foreground)
      .child(v_flex().gap(2)
        .child(div().font_semibold().text_size(14).child("Resume Claude sessions"))
        .child(div().text_size(12).text_color(t.muted)
          .child("These terminals ran Claude Code before Herdr restarted them. Resuming reloads each conversation, which uses tokens.")))
      .child(v_flex().id("list").flex_1().gap(2).overflow_y_scrollbar()
        .when(this.rows === null, (el) => el.child(div().text_size(12).text_color(t.muted).child("Looking...")))
        .when(this.rows !== null && rows.length === 0, (el) => el.child(div().text_size(12).text_color(t.muted)
          .child("Nothing to resume. A session shows up here when its terminal is back as a plain shell.")))
        .children(rows.map(row)))
      .when(Boolean(this.err), (el) => el.child(div().text_size(11.5).text_color(t.blocked).truncate().child(this.err)))
      .when(rows.length > 0, (el) => el.child(h_flex().gap(8).justify_end().items_center()
        .when(Boolean(total), (el) => el.child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(t.muted)
          .tooltip(CACHE_TIP).child(total)))
        .child(button("dismiss", "Dismiss", false, !this.busy, (_e, cx) => this.dismiss(cx)))
        .child(button("resume", picked === rows.length ? "Resume all" : `Resume ${picked}`, true, !this.busy && picked > 0, (_e, cx) => this.resume(cx)))))
      .child(hooks);
  }
}
