/**
 * 页眉：品牌、全局搜索、新建任务与账号入口。
 *
 * 白底、高度固定，与主体之间只有一条极浅的分隔线——页眉是工具条，不是主视觉。
 *
 * 搜索框的现状必须说清楚：后端没有搜索接口，它过滤的是侧栏「最近任务」这一个列表
 * （`GET /api/tasks?limit=20`），因此文案写的是「筛选最近任务」而不是「搜索全部任务」。
 * 等后端提供检索能力时，这里换成真正的查询即可，界面承诺不需要改。
 *
 * 账号区自 V0.5 起接的是**真实登录态**：身份来自 HttpOnly Cookie 背后的会话，
 * 退出登录会真的请求后端把会话作废。这里不再有「角色」——本版没有角色机制，
 * 显示一个永远相同的角色只会让人误以为存在权限差异。
 */

import { useEffect, useRef, useState } from "react";
import { IconChevronDown, IconLogout, IconPlus, IconSearch } from "./icons";
import type { SessionUser } from "./session";

export default function Header({
  user,
  query,
  onQuery,
  onNewTask,
  onSignOut,
}: {
  /** 已登录身份。工作台只在登录后渲染，因此这里不为空。 */
  user: SessionUser;
  /** 侧栏「最近任务」的筛选词 */
  query: string;
  onQuery: (value: string) => void;
  onNewTask: () => void;
  onSignOut: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const accountRef = useRef<HTMLDivElement>(null);

  // 点击别处或按 Esc 收起账号菜单；菜单只是快捷方式，不做焦点陷阱
  useEffect(() => {
    if (!menuOpen) return;
    const onPointer = (event: MouseEvent) => {
      if (!accountRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("mousedown", onPointer);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onPointer);
      window.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  return (
    <header className="masthead">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" width={14} height={14} aria-hidden="true">
            <path d="M9 6.5l8.5 5.5L9 17.5z" fill="currentColor" />
          </svg>
        </span>
        <h1 className="brand-name">LiveReview</h1>
        <span className="brand-scope">直播复盘工作台</span>
      </div>

      <div className="masthead-search">
        <span className="masthead-search-icon" aria-hidden="true">
          <IconSearch size={16} />
        </span>
        <input
          type="search"
          value={query}
          onChange={(event) => onQuery(event.target.value)}
          placeholder="筛选最近任务：文件名、店铺等…"
          aria-label="筛选最近任务"
        />
      </div>

      <div className="masthead-actions">
        <button type="button" className="btn btn--primary" onClick={onNewTask}>
          <IconPlus size={16} />
          新建任务
        </button>

        <div className="account" ref={accountRef}>
          <button
            type="button"
            className="account-trigger"
            onClick={() => setMenuOpen((open) => !open)}
            aria-expanded={menuOpen}
            aria-haspopup="menu"
          >
            <span className="avatar" aria-hidden="true">
              {user.display_name.slice(0, 1)}
            </span>
            <span className="account-name">{user.display_name}</span>
            <IconChevronDown size={16} />
          </button>

          {menuOpen && (
            <div className="account-menu" role="menu">
              {/* 显示名可能与账号不同（如「测试用户」与 tester），两行都给出来便于核对身份 */}
              <p className="account-menu-role">
                {user.display_name === user.username ? user.username : `${user.username} 已登录`}
              </p>
              <button
                type="button"
                className="account-menu-item"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  onSignOut();
                }}
              >
                <IconLogout size={16} />
                退出登录
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
