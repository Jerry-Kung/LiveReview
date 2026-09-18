/**
 * 图标集：细线风格（stroke 1.5）的统一图标。
 *
 * 全部内联为 SVG，不引入图标库——本项目的图标用量有限，加一个依赖的收益不抵它的体积与
 * 版本负担。尺寸只取 16 / 18 / 20 三档，线宽与端点在所有图标上保持一致，避免混用不同
 * 图标风格。
 *
 * 取值约定：图标默认继承 `currentColor`，颜色由使用处决定，图标本身不携带语义色。
 */

type IconProps = {
  /** 边长，只取 16 / 18 / 20 三档 */
  size?: 16 | 18 | 20;
  className?: string;
};

const BOX = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

// ── 导航 ─────────────────────────────────────────────────────────────────

export function IconList({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <rect x="3" y="4.5" width="18" height="15" rx="2" />
      <path d="M3 9.5h18M8 9.5V19.5" />
    </svg>
  );
}

export function IconChart({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <path d="M4 20h16" />
      <rect x="6" y="12" width="3.5" height="5.5" rx="1" />
      <rect x="11" y="8" width="3.5" height="9.5" rx="1" />
      <rect x="16" y="14" width="3.5" height="3.5" rx="1" />
    </svg>
  );
}

export function IconSettings({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3.5l1.3 2.2 2.5-.5.4 2.5 2.2 1.3-1.2 2.2 1.2 2.2-2.2 1.3-.4 2.5-2.5-.5L12 20.5l-1.3-2.2-2.5.5-.4-2.5L5.6 15l1.2-2.2L5.6 10.6l2.2-1.3.4-2.5 2.5.5z" />
    </svg>
  );
}

// ── 动作 ─────────────────────────────────────────────────────────────────

export function IconPlus({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <path d="M12 5.5v13M5.5 12h13" />
    </svg>
  );
}

export function IconSearch({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M15.8 15.8L20.5 20.5" />
    </svg>
  );
}

export function IconChevronDown({ size = 16, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <path d="M6.5 9.5l5.5 5.5 5.5-5.5" />
    </svg>
  );
}

export function IconArrowLeft({ size = 16, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <path d="M19 12H5.5M11 6l-6 6 6 6" />
    </svg>
  );
}

export function IconChevronRight({ size = 16, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <path d="M9.5 6l5.5 6-5.5 6" />
    </svg>
  );
}

export function IconMore({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <circle cx="5.5" cy="12" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="18.5" cy="12" r="1.2" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function IconClose({ size = 16, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />
    </svg>
  );
}

export function IconRefresh({ size = 16, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <path d="M20 12a8 8 0 1 1-2.3-5.6" />
      <path d="M20 4.5V9h-4.5" />
    </svg>
  );
}

// ── 内容与媒体 ───────────────────────────────────────────────────────────

export function IconUploadCloud({ size = 20, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <path d="M7 17.5a4.5 4.5 0 0 1-.4-9A6 6 0 0 1 18 9.6a3.95 3.95 0 0 1-.6 7.9" />
      <path d="M12 12v7.5M9 15l3-3 3 3" />
    </svg>
  );
}

export function IconFile({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <path d="M14 3.5H7.5a1.5 1.5 0 0 0-1.5 1.5v14a1.5 1.5 0 0 0 1.5 1.5h9a1.5 1.5 0 0 0 1.5-1.5V8z" />
      <path d="M14 3.5V8h4.5M9 13h6M9 16.5h4" />
    </svg>
  );
}

export function IconVideo({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <rect x="3" y="5.5" width="18" height="13" rx="2" />
      <path d="M10.5 9.5l4.5 2.5-4.5 2.5z" />
    </svg>
  );
}

export function IconMedia({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <path d="M3.5 15l4-4 3.5 3.5L14.5 11l6 5.5" />
      <circle cx="9" cy="9" r="1.2" />
    </svg>
  );
}

export function IconGrid({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <path d="M3.5 12h17M12 4.5v15" />
    </svg>
  );
}

export function IconVoice({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <rect x="9" y="3.5" width="6" height="10" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3" />
    </svg>
  );
}

export function IconReport({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <path d="M14 3.5H7.5a1.5 1.5 0 0 0-1.5 1.5v14a1.5 1.5 0 0 0 1.5 1.5h9a1.5 1.5 0 0 0 1.5-1.5V8z" />
      <path d="M14 3.5V8h4.5M9 16.5v-3M12 16.5v-6M15 16.5v-4" />
    </svg>
  );
}

export function IconInfo({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5.5M12 7.8v.4" />
    </svg>
  );
}

// ── 状态 ─────────────────────────────────────────────────────────────────

export function IconCheck({ size = 16, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <path d="M5.5 12.5l4 4 9-9" />
    </svg>
  );
}

export function IconCheckCircle({ size = 16, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M8.3 12.3l2.6 2.6 4.8-5" />
    </svg>
  );
}

export function IconAlert({ size = 16, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.8v5.4M12 16.2v.4" />
    </svg>
  );
}

export function IconUser({ size = 18, className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" {...BOX}>
      <circle cx="12" cy="9" r="3.5" />
      <path d="M5.5 19.5a6.5 6.5 0 0 1 13 0" />
    </svg>
  );
}
