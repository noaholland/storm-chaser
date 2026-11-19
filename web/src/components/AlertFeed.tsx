import { STATIC } from "../api";
import type { Storm } from "../useStorm";
import { COMPONENT_LABEL, LEVELS, LEVEL_COLOR, fmtPrice, fmtTime, isLevel } from "../format";

export function AlertFeed({ storm }: { storm: Storm }) {
  const { transitions, alerts, symbol, health, snap, apiState } = storm;
  const telegram = !!health?.notifiers.includes("telegram");
  const mine = alerts.filter((a) => a.symbol === symbol && (!snap || a.wall_time >= snap.session.started - 1));
  const stale = mine.filter((a) => a.kind === "stale");
  const sent = mine.length - stale.length;
  const items = [...transitions].reverse().slice(0, 150);
  return (
    <>
      <div className="row space" style={{ padding: "8px 12px", borderBottom: "1px solid var(--line)" }}>
        <span className="small muted" data-testid="alert-count">
          {transitions.length} level change{transitions.length === 1 ? "" : "s"} · {sent} alert{sent === 1 ? "" : "s"} sent
        </span>
        <span
          className="chip"
          data-on={telegram}
          data-testid="telegram"
          title={
            telegram
              ? "Warning and tornado alerts are pushed to Telegram"
              : STATIC
                ? "Static demo: Telegram alerts need the Python backend (run locally)"
                : "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID on the server to push alerts"
          }
        >
          Telegram {telegram ? "on" : "off"}
        </span>
      </div>
      <ul className="feed" data-testid="alert-feed">
        {stale.map((a) => (
          <li key={`stale-${a.wall_time}`} style={{ ["--c" as string]: LEVEL_COLOR.stale }}>
            <i />
            <div>
              <div className="feed-top">
                <span className="feed-move">Feed stale</span>
                <span className="feed-time">{new Date(a.wall_time * 1000).toLocaleTimeString("en-GB")}</span>
              </div>
              <div className="feed-detail">No ticks for {Math.round(a.age_s ?? 0)} s. Score not reliable.</div>
            </div>
          </li>
        ))}
        {items.map((t) => {
          const up = LEVELS.indexOf(t.to as never) > LEVELS.indexOf(t.frm as never);
          const col = isLevel(t.to) ? LEVEL_COLOR[t.to] : "var(--ink-3)";
          return (
            <li key={`${t.ts}|${t.frm}|${t.to}`} data-new="true" data-to={t.to} style={{ ["--c" as string]: up ? col : "var(--ink-3)" }}>
              <i />
              <div>
                <div className="feed-top">
                  <span className="feed-move">
                    <small>{t.frm} to </small>
                    {t.to}
                  </span>
                  <span className="feed-time">{fmtTime(t.ts)}</span>
                </div>
                <div className="feed-detail">
                  score {Math.round(t.score)} · {fmtPrice(t.price)} · {COMPONENT_LABEL[t.why]?.toLowerCase() ?? t.why}
                  {t.kind !== "calm" ? ` · ${t.kind}` : ""}
                  {t.carry_unwind && (
                    <>
                      {" "}
                      · <b>carry unwind</b>
                    </>
                  )}
                </div>
              </div>
            </li>
          );
        })}
        {items.length === 0 && stale.length === 0 && (
          <li style={{ display: "block", borderBottom: 0 }}>
            <div className="empty" data-testid="alerts-empty">
              {apiState === "down"
                ? "Alert log unavailable while the API is offline."
                : symbol
                  ? "No level changes yet. Every move between clear, watch, warning and tornado lands here as it happens."
                  : "Alerts appear here once a feed is running."}
            </div>
          </li>
        )}
      </ul>
    </>
  );
}
