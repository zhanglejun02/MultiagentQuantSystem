import { ReactNode } from "react";

export interface Column<T> {
  key: string;
  title: string;
  render?: (row: T) => ReactNode;
  align?: "left" | "right";
}

export default function DataTable<T extends Record<string, any>>({
  columns,
  rows,
  rowKey,
  empty = "暂无数据",
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  empty?: string;
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} style={{ textAlign: c.align ?? "left" }}>
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={columns.length} className="empty">
                {empty}
              </td>
            </tr>
          )}
          {rows.map((r) => (
            <tr key={rowKey(r)}>
              {columns.map((c) => (
                <td key={c.key} style={{ textAlign: c.align ?? "left" }}>
                  {c.render ? c.render(r) : String(r[c.key] ?? "-")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
