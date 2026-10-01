import {
  Activity,
  BarChart3,
  BriefcaseBusiness,
  CandlestickChart,
  FlaskConical,
  LayoutDashboard,
  ReceiptText,
  ShieldCheck,
  Sigma,
  TrendingUp,
} from "lucide-react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import Overview from "./pages/Overview";
import Portfolio from "./pages/Portfolio";
import OrdersTrades from "./pages/OrdersTrades";
import Research from "./pages/Research";
import Backtest from "./pages/Backtest";
import SystemRisk from "./pages/SystemRisk";
import Factors from "./pages/Factors";
import Futures from "./pages/Futures";
import Market from "./pages/Market";

const NAV = [
  { to: "/overview", label: "总览", icon: LayoutDashboard },
  { to: "/market", label: "市场", icon: CandlestickChart },
  { to: "/portfolio", label: "投资组合", icon: BriefcaseBusiness },
  { to: "/orders", label: "订单与成交", icon: ReceiptText },
  { to: "/research", label: "研究", icon: FlaskConical },
  { to: "/factors", label: "Alpha Lab", icon: Sigma },
  { to: "/backtest", label: "回测", icon: BarChart3 },
  { to: "/futures", label: "期货仿真", icon: TrendingUp },
  { to: "/system", label: "系统与风控", icon: ShieldCheck },
];

export default function App() {
  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark"><img src="/brand/jj-mark.png" alt="" /></span>
          <span className="brand-lockup"><strong>骏杰资本</strong><small>JJ CAPITAL</small></span>
        </div>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} title={n.label} aria-label={n.label} className={({ isActive }) => (isActive ? "active" : "")}>
              <n.icon size={16} strokeWidth={1.8} />
              <span>{n.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-brand-statement" aria-hidden="true">
          <img src="/characters/gengar.png" alt="" />
          <span>DISCIPLINE · INSIGHT<br />LONG-TERM VALUE</span>
        </div>
        <div className="sidebar-foot"><Activity size={13} /> 仅模拟交易</div>
      </aside>
      <div className="mobile-brand" aria-label="骏杰资本 JJ Capital">
        <img src="/brand/jj-mark.png" alt="" />
        <span><strong>骏杰资本</strong><small>JJ CAPITAL</small></span>
      </div>
      <main className="content">
        <Routes>
          <Route path="/" element={<Navigate to="/overview" replace />} />
          <Route path="/overview" element={<Overview />} />
          <Route path="/market" element={<Market />} />
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/orders" element={<OrdersTrades />} />
          <Route path="/research" element={<Research />} />
          <Route path="/factors" element={<Factors />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/futures" element={<Futures />} />
          <Route path="/system" element={<SystemRisk />} />
        </Routes>
      </main>
    </div>
  );
}
