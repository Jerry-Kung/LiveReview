/**
 * 用户登录弹层。
 *
 * 预留功能区：账号体系与鉴权尚未实现，这里只承载界面状态。提交不会请求后端，
 * 界面会明确说明这一点，避免让用户以为已经完成真实登录。
 */

import { useEffect, useRef, useState } from "react";

export const ROLES = ["直播运营", "内容复盘", "投放管理"] as const;

export default function LoginDialog({
  open,
  onClose,
  onSignIn,
}: {
  open: boolean;
  onClose: () => void;
  onSignIn: (user: { name: string; role: string }) => void;
}) {
  const [name, setName] = useState("");
  const [role, setRole] = useState<string>(ROLES[0]);
  const [notice, setNotice] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  // 打开时聚焦到第一个输入框，关闭时清掉上一次的提示
  useEffect(() => {
    if (open) {
      setNotice(null);
      nameRef.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const trimmed = name.trim();

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (trimmed === "") {
      setNotice("请填写登录账号。");
      return;
    }
    onSignIn({ name: trimmed, role });
    setNotice(null);
  };

  return (
    <div className="overlay" role="presentation" onClick={onClose}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="login-title"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 className="dialog-title" id="login-title">
          用户登录
        </h2>
        <p className="dialog-note">
          登录功能为预留功能区。当前版本不校验账号、不请求后端，填写后仅用于界面显示。
        </p>

        <form className="dialog-form" onSubmit={submit}>
          <label className="field">
            <span className="field-label">账号</span>
            <input
              ref={nameRef}
              className="field-input"
              type="text"
              value={name}
              placeholder="姓名或企业邮箱"
              onChange={(event) => setName(event.target.value)}
            />
          </label>

          <label className="field">
            <span className="field-label">角色</span>
            <select
              className="field-input"
              value={role}
              onChange={(event) => setRole(event.target.value)}
            >
              {ROLES.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>

          {notice && <p className="field-notice">{notice}</p>}

          <div className="actions">
            <button type="submit" className="btn btn--primary">
              进入工作台
            </button>
            <button type="button" className="btn" onClick={onClose}>
              取消
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
