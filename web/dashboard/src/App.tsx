import {
  Activity,
  BarChart3,
  BriefcaseBusiness,
  FlaskConical,
  LayoutDashboard,
  ReceiptText,
  ShieldCheck,
  Sigma,
} from "lucide-react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import Overview from "./pages/Overview";
import Portfolio from "./pages/Portfolio";
import OrdersTrades from "./pages/OrdersTrades";
import Research from "./pages/Research";
import Backtest from "./pages/Backtest";
import SystemRisk from "./pages/SystemRisk";
import Factors from "./pages/Factors";

const NAV = [
  { to: "/overview", label: "Overview", icon: LayoutDashboard },
  { to: "/portfolio", label: "Portfolio", icon: BriefcaseBusiness },
  { to: "/orders", label: "Orders & Trades", icon: ReceiptText },
  { to: "/research", label: "Research", icon: FlaskConical },
  { to: "/factors", label: "Alpha Lab", icon: Sigma },
  { to: "/backtest", label: "Backtest", icon: BarChart3 },
  { to: "/system", label: "System / Risk", icon: ShieldCheck },
];

export default function App() {
  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark"><img src="/characters/gengar.png" alt="" /></span>
          <span>quant<span className="brand-dot">.</span>system</span>
        </div>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? "active" : "")}>
              <n.icon size={16} strokeWidth={1.8} />
              <span>{n.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-character" aria-hidden="true">
          <span className="sidebar-sparkle sparkle-one">✦</span>
          <span className="sidebar-sparkle sparkle-two">·</span>
          <img src="/characters/gengar.png" alt="" />
        </div>
        <div className="sidebar-foot"><Activity size={13} /> paper trading only</div>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<Navigate to="/overview" replace />} />
          <Route path="/overview" element={<Overview />} />
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/orders" element={<OrdersTrades />} />
          <Route path="/research" element={<Research />} />
          <Route path="/factors" element={<Factors />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/system" element={<SystemRisk />} />
        </Routes>
      </main>
    </div>
  );
}
