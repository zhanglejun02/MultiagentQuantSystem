import * as echarts from "echarts";
import { useEffect, useRef } from "react";

export const chartColors = {
  purple: "#704087",
  pink: "#d56898",
  red: "#d6536b",
  green: "#278460",
};

export function areaGradient(color: string, opacity = 0.24) {
  return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
    { offset: 0, color: echarts.color.modifyAlpha(color, opacity) },
    { offset: 1, color: echarts.color.modifyAlpha(color, 0.015) },
  ]);
}

echarts.registerTheme("quant-system", {
  color: [chartColors.purple, chartColors.pink, chartColors.red, chartColors.green],
  textStyle: {
    color: "#807186",
    fontFamily: '"Avenir Next", "PingFang SC", sans-serif',
  },
  categoryAxis: {
    axisLine: { lineStyle: { color: "#d8cbdc" } },
    axisTick: { show: false },
    axisLabel: { color: "#8a7b90", margin: 12 },
    splitLine: { show: false },
  },
  valueAxis: {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: "#8a7b90", margin: 10 },
    splitLine: { lineStyle: { color: "#eee6f0", type: "dashed" } },
  },
  line: {
    smooth: 0.22,
    symbol: "circle",
    symbolSize: 6,
    lineStyle: { width: 2.2 },
    emphasis: { lineStyle: { width: 3 } },
  },
  bar: {
    itemStyle: { borderRadius: [3, 3, 0, 0] },
  },
  tooltip: {
    backgroundColor: "rgba(53, 43, 61, 0.94)",
    borderColor: "#704087",
    borderWidth: 1,
    textStyle: { color: "#fff", fontSize: 12 },
    axisPointer: { lineStyle: { color: "#b893c5", type: "dashed" } },
    extraCssText: "border-radius:7px;box-shadow:0 8px 24px rgba(53,43,61,.2);",
  },
  animationDuration: 650,
  animationDurationUpdate: 300,
  animationEasing: "cubicOut",
});

export default function Chart({
  option,
  height = 320,
}: {
  option: echarts.EChartsOption;
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts>();

  useEffect(() => {
    if (!ref.current) return;
    chart.current = echarts.init(ref.current, "quant-system");
    const onResize = () => chart.current?.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.current?.dispose();
    };
  }, []);

  useEffect(() => {
    chart.current?.setOption(option, true);
  }, [option]);

  return <div ref={ref} style={{ height, width: "100%" }} />;
}
