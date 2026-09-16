import { useEffect, useState } from "react";
import { Artifact, api } from "./api";

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fn()
      .then((d) => alive && (setData(d), setError(null)))
      .catch((e) => alive && setError(String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading };
}

/** 回测选择器状态：默认选最新一次回测 */
export function useBacktestSelection() {
  const { data: backtests, error } = useAsync<Artifact[]>(() => api.backtests(), []);
  const [selected, setSelected] = useState<string | null>(null);
  const id = selected ?? backtests?.[0]?.artifact_id ?? null;
  return { backtests: backtests ?? [], id, setSelected, error };
}

/** 从 equity 序列计算日收益、累计收益与回撤 */
export function equityDerived(navs: number[]) {
  const daily: number[] = [];
  const cum: number[] = [];
  const dd: number[] = [];
  let peak = navs[0] ?? 0;
  for (let i = 0; i < navs.length; i++) {
    daily.push(i === 0 ? 0 : navs[i] / navs[i - 1] - 1);
    cum.push(navs[0] ? navs[i] / navs[0] - 1 : 0);
    peak = Math.max(peak, navs[i]);
    dd.push(peak ? navs[i] / peak - 1 : 0);
  }
  return { daily, cum, dd };
}
