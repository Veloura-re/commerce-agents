import { useState, useEffect } from 'react'
import { Brain, Activity, ShieldAlert, TrendingUp, History, Network, CheckCircle2, XCircle } from 'lucide-react'

const electron = (window as any).require ? (window as any).require('electron') : null;

function App() {
  const [state, setState] = useState<any>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [weights, setWeights] = useState<any>(null);

  useEffect(() => {
    const fetchData = async () => {
      if (electron) {
        const audit = await electron.ipcRenderer.invoke('get-state');
        if (audit) setState(audit);
        
        const rawLogs = await electron.ipcRenderer.invoke('get-logs');
        if (rawLogs) setLogs(rawLogs);
        
        const rawWeights = await electron.ipcRenderer.invoke('get-weights');
        if (rawWeights) setWeights(rawWeights);
      }
    };

    fetchData();
    const interval = setInterval(fetchData, 1000);
    return () => clearInterval(interval);
  }, []);

  const getVetoes = () => {
    const vetoes: any[] = [];
    [...logs].reverse().forEach(line => {
      if (line.includes("COUNCIL VETO")) {
        const match = line.match(/COUNCIL VETO \(([^)]+)\): (.+)/);
        if (match && vetoes.length < 20) {
          vetoes.push({ sym: match[1], reason: match[2], id: line });
        }
      }
    });
    return vetoes.filter((v, i, a) => a.findIndex(t => (t.sym === v.sym)) === i);
  };

  const isBrainActive = logs.slice(-5).some(l => l.includes("COUNCIL"));
  const vetoList = getVetoes();

  const totalPnL = state?.total_realized_pnl_sol || 0;
  const winRate = state?.win_rate_pct || 0;
  const profitFactor = state?.profit_factor || 0;
  const totalTrades = state?.total_trades_closed || 0;
  
  const activePositions = state?.active_positions || [];
  const closedTrades = state?.closed_trades ? [...state.closed_trades].reverse().slice(0, 15) : [];

  return (
    <>
      <div className="top-bar" style={{ display: 'flex', gap: '2rem', alignItems: 'center' }}>
        <div className="logo" style={{ minWidth: '200px' }}>
          <Brain size={28} className="logo-accent" />
          KIDA <span style={{ fontWeight: 300, color: 'var(--text-muted)' }}>Citadel</span>
        </div>
        
        <div style={{ display: 'flex', gap: '1rem', flex: 1, justifyContent: 'center' }}>
          <div className="glass-panel stat-panel">
            <span className="stat-label">Realized PnL</span>
            <span className={`stat-value mono ${totalPnL >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
              {totalPnL > 0 ? '+' : ''}{totalPnL.toFixed(4)} SOL
            </span>
          </div>
          <div className="glass-panel stat-panel">
            <span className="stat-label">Win Rate</span>
            <span className="stat-value mono">{winRate.toFixed(1)}%</span>
          </div>
          <div className="glass-panel stat-panel">
            <span className="stat-label">Profit Factor</span>
            <span className="stat-value mono">{profitFactor.toFixed(2)}</span>
          </div>
          <div className="glass-panel stat-panel">
            <span className="stat-label">Closed Trades</span>
            <span className="stat-value mono">{totalTrades}</span>
          </div>
        </div>
      </div>

      <div className="main-content" style={{ display: 'grid', gridTemplateColumns: '300px 1fr 300px', gap: '1.5rem', padding: '1.5rem', height: 'calc(100vh - 80px)', boxSizing: 'border-box' }}>
        
        {/* LEFT COLUMN: Brain & Active Slots */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem', overflowY: 'auto' }}>
          <div className="glass-panel brain-container" style={{ padding: '2rem' }}>
            <div className={`brain-core ${isBrainActive ? '' : 'idle'}`}>
               <Activity size={48} color={isBrainActive ? 'var(--accent-emerald)' : 'var(--text-muted)'} />
               <div className="brain-label" style={{ marginTop: '1rem', fontSize: '1rem' }}>
                 {isBrainActive ? 'Council Computing' : 'Scanning Mempool'}
               </div>
            </div>
          </div>

          <div style={{ fontWeight: 500, letterSpacing: '2px', fontSize: '0.8rem', color: 'var(--text-muted)' }}>ACTIVE POSITIONS</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            {activePositions.length === 0 ? (
              <div className="glass-panel" style={{ textAlign: 'center', opacity: 0.5, padding: '2rem' }}>
                No active positions.
              </div>
            ) : (
              activePositions.map((pos: any, idx: number) => {
                const pnl = pos.unrealized_pnl_pct * 100;
                return (
                  <div key={idx} className="glass-panel slot-card">
                    <div className="slot-header">
                      <span>{pos.symbol}</span>
                      <TrendingUp size={16} className={pnl >= 0 ? 'logo-accent' : ''} color={pnl < 0 ? 'var(--accent-red)' : undefined}/>
                    </div>
                    <div className={`slot-pnl mono ${pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
                      {pnl > 0 ? '+' : ''}{pnl.toFixed(2)}%
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                      <span>Size: {pos.size_sol} SOL</span>
                      <span>Floor: +{(pos.stop_loss_pct * 100).toFixed(1)}%</span>
                    </div>
                  </div>
                )
              })
            )}
          </div>
        </div>

        {/* CENTER COLUMN: Backprop & History */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem', overflowY: 'hidden' }}>
          
          <div className="glass-panel" style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontWeight: 600, color: 'var(--accent-emerald)' }}>
              <Network size={20} /> Neural Heuristics (Backprop)
            </div>
            {weights ? (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '1rem' }}>
                <div className="stat-panel" style={{ background: 'rgba(255,255,255,0.02)' }}>
                  <span className="stat-label">Smart Money</span>
                  <span className="stat-value mono">{(weights.w_smart_money * 100).toFixed(1)}%</span>
                </div>
                <div className="stat-panel" style={{ background: 'rgba(255,255,255,0.02)' }}>
                  <span className="stat-label">Liquidity</span>
                  <span className="stat-value mono">{(weights.w_liquidity * 100).toFixed(1)}%</span>
                </div>
                <div className="stat-panel" style={{ background: 'rgba(255,255,255,0.02)' }}>
                  <span className="stat-label">Momentum</span>
                  <span className="stat-value mono">{(weights.w_momentum * 100).toFixed(1)}%</span>
                </div>
                <div className="stat-panel" style={{ background: 'rgba(255,255,255,0.02)' }}>
                  <span className="stat-label">Buy Ratio</span>
                  <span className="stat-value mono">{(weights.w_buy_ratio * 100).toFixed(1)}%</span>
                </div>
              </div>
            ) : (
              <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>Awaiting weight optimization sync...</div>
            )}
          </div>

          <div className="glass-panel" style={{ flex: 1, padding: '1.5rem', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontWeight: 600 }}>
              <History size={20} /> Trade Ledger
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
              {closedTrades.length === 0 ? (
                 <div style={{ color: 'var(--text-muted)', padding: '1rem' }}>No closed trades yet.</div>
              ) : (
                closedTrades.map((t: any, i: number) => {
                  const p = (t.pnl_pct * 100).toFixed(2);
                  const isWin = t.pnl_pct > 0;
                  return (
                    <div key={i} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0.75rem', background: 'rgba(255,255,255,0.02)', borderRadius: '8px', borderLeft: `3px solid ${isWin ? 'var(--accent-emerald)' : 'var(--accent-red)'}` }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                        {isWin ? <CheckCircle2 size={16} color="var(--accent-emerald)" /> : <XCircle size={16} color="var(--accent-red)" />}
                        <span style={{ fontWeight: 500 }}>{t.symbol}</span>
                        <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>{t.reason.split(' ')[0]}</span>
                      </div>
                      <div className="mono" style={{ color: isWin ? 'var(--accent-emerald)' : 'var(--accent-red)' }}>
                        {isWin ? '+' : ''}{p}%
                      </div>
                    </div>
                  )
                })
              )}
            </div>
          </div>
        </div>

        {/* RIGHT COLUMN: Vetoes */}
        <div className="glass-panel veto-sidebar" style={{ height: '100%', overflowY: 'auto' }}>
          <div className="sidebar-title" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', position: 'sticky', top: 0, background: 'var(--bg-panel)', paddingBottom: '1rem' }}>
            <ShieldAlert size={16} /> Council Vetoes
          </div>
          <div className="veto-list">
            {vetoList.map((veto, i) => (
              <div key={veto.id + i} className="veto-item">
                <div style={{ flex: 1 }}>
                  <div className="veto-sym" style={{ color: 'var(--accent-red)' }}>{veto.sym}</div>
                  <div className="veto-reason" style={{ fontSize: '0.75rem', lineHeight: '1.4' }}>{veto.reason}</div>
                </div>
              </div>
            ))}
            {vetoList.length === 0 && (
              <div style={{ color: 'var(--text-muted)', fontSize: '0.875rem' }}>No recent vetoes.</div>
            )}
          </div>
        </div>
        
      </div>
    </>
  )
}

export default App
