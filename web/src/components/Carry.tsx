import type { Storm } from "../useStorm";
import { fmtPct } from "../format";

const TENORS = ["1W", "2W", "3W", "1M", "2M", "3M", "6M", "9M", "1Y"];

/** Forward curve as an implied interest-rate differential per tenor (annualised), with the carry-unwind flag. */
export function Carry({ storm }: { storm: Storm }) {
  const snap = storm.snap;
  const carry = snap?.carry;
  const hasCurve = !!carry && Object.keys(carry.curve || {}).length > 0;
  if (!snap || !hasCurve) {
    const expected = (snap?.session.curve_tenors.length ?? 0) > 0;
    return (
      <div className="carry" data-testid="carry">
        <div className="empty small" data-testid="carry-empty">
          {expected
            ? "Forward curve loading: tenors arrive with the first bars."
            : "No forward curve on this feed. The SGX sets carry 1W to 1Y forward points; pick one to see the implied rate differential."}
        </div>
      </div>
    );
  }
  const tenors = TENORS.filter((t) => t in carry.curve).concat(Object.keys(carry.curve).filter((t) => !TENORS.includes(t)));
  const vals = tenors.map((t) => carry.curve[t]);
  const lo = Math.min(...vals, 0);
  const hi = Math.max(...vals, 0);
  const pad = (hi - lo || 0.001) * 0.18;
  const W = 300, H = 118, L = 6, Rr = 6, T = 20, B = 20;
  const y = (v: number) => T + ((hi + pad - v) / (hi - lo + 2 * pad)) * (H - T - B);
  const bw = (W - L - Rr) / tenors.length;
  const y0 = y(0);
  const side = carry.earning_side;
  return (
    <div className="carry" data-testid="carry">
      <div className="carry-head">
        <span className="flag" data-on={carry.unwind} data-testid="carry-unwind">
          {carry.unwind ? "Carry unwind" : "No unwind"}
        </span>
        <span className="small muted">
          {side ? `${side}s earn ` : ""}
          <b className="num" style={{ color: "var(--ink)" }}>{fmtPct(Math.abs(carry.median ?? 0), 2)}</b> a year
          {carry.dislocation_z !== null && <> · curve z <span className="num">{carry.dislocation_z.toFixed(1)}</span></>}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Implied rate differential per tenor">
        <line x1={L} x2={W - Rr} y1={y0} y2={y0} stroke="rgba(180,205,194,0.5)" strokeWidth="1" />
        {tenors.map((t, i) => {
          const v = carry.curve[t];
          const top = Math.min(y(v), y0);
          const h = Math.max(1.5, Math.abs(y(v) - y0));
          const x = L + i * bw;
          return (
            <g key={t}>
              <rect x={x + 3} y={top} width={bw - 6} height={h} fill={carry.unwind ? "var(--tornado)" : "#5fb7d8"} opacity="0.85" />
              <text x={x + bw / 2} y={v < 0 ? top + h + 11 : top - 4} textAnchor="middle" fontSize="9.5" fontFamily="var(--mono)" fill="#ecf7f1">
                {(v * 100).toFixed(2)}
              </text>
              <text x={x + bw / 2} y={v < 0 ? y0 - 5 : y0 + 12} textAnchor="middle" fontSize="10" fontFamily="var(--mono)" fill="#7d998e">
                {t}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="small muted">Implied rate differential, % a year, from forward points (fp / spot x 365 / days).</div>
    </div>
  );
}
