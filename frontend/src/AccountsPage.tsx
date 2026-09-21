/**
 * 账号管理（V0.5.2）：列出全部账号、新增普通账号、删除普通账号。
 *
 * 只有管理员能进到这里（入口由侧栏按角色渲染，直接敲 `#/accounts` 会看到说明页），
 * 真正的边界在后端：这一组接口对非管理员一律 403。
 *
 * 两条来自后端的约定，界面如实反映而不是自己另立一套：
 *
 * 1. **这里建出来的账号一律是普通账号**。表单里没有角色选项——服务端也不接受角色字段，
 *    让使用者以为自己能造管理员，只会在提交后收获一个 403 或一个不符合预期的账号。
 * 2. **管理员账号不可删除**。这些行不渲染删除按钮；`window.confirm` 只出现在能删的行上。
 *
 * 删除确认用浏览器原生对话框，与任务删除处一致：本项目刻意没有弹窗组件，为一个确认框
 * 引入第一个 modal 不划算（登录页那段注释记着同样的取舍）。
 */

import { useCallback, useEffect, useState } from "react";
import { createAccount, deleteAccount, fetchAccounts, type Account } from "./api";
import { PLACEHOLDER, formatMoment } from "./format";

/** 角色的中文说明。未知取值原样显示，不假装认识它。 */
const ROLE_LABELS: Record<string, string> = {
  admin: "管理员",
  member: "普通账号",
};

function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

/** 角色用小圆点 + 文字表达，与项目里状态的处理方式一致，不用彩色徽章。 */
function roleTone(role: string): "ok" | "busy" | undefined {
  if (role === "admin") return "ok";
  if (role === "member") return "busy";
  return undefined;
}

export default function AccountsPage() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  // 正在删除的那一行；删除是逐行的，按钮的禁用状态要跟着它走
  const [removingId, setRemovingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchAccounts();
      setAccounts(data.items);
      setListError(null);
    } catch (err) {
      setListError(err instanceof Error ? err.message : "账号列表读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (pending) return;
    const account = username.trim();
    if (account === "" || password === "") {
      setFormError("请填写账号与密码。");
      return;
    }
    setPending(true);
    setFormError(null);
    try {
      await createAccount(account, password, displayName.trim());
      // 建成即重取名册，而不是把返回值插进本地数组：清单里还有 id 与创建时间这些
      // 只有服务端才知道的字段，以后端为准确。
      await load();
      // 清空表单，但保留刚填的显示名——连续建几个同称呼的账号是常见情形
      setUsername("");
      setPassword("");
    } catch (err) {
      // 与登录页一致：失败后保留账号，只清口令
      setPassword("");
      setFormError(err instanceof Error ? err.message : "创建账号失败");
    } finally {
      setPending(false);
    }
  };

  const remove = async (account: Account) => {
    const confirmed = window.confirm(
      `删除账号「${account.username}」后无法恢复，该账号已登录的会话会立即失效。确认删除？`,
    );
    if (!confirmed) return;
    setRemovingId(account.id);
    setListError(null);
    try {
      await deleteAccount(account.id);
      await load();
    } catch (err) {
      setListError(err instanceof Error ? err.message : "删除账号失败");
    } finally {
      setRemovingId(null);
    }
  };

  return (
    <div className="page">
      <h2 className="page-title">账号管理</h2>

      <div className="panel">
        <div className="panel-head">
          <h3 className="panel-title">新增账号</h3>
        </div>
        <form className="account-form" onSubmit={submit}>
          <label className="field">
            <span className="field-label">账号</span>
            <input
              className="field-input"
              type="text"
              name="username"
              autoComplete="off"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>

          <label className="field">
            <span className="field-label">显示名</span>
            <input
              className="field-input"
              type="text"
              name="display-name"
              autoComplete="off"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </label>

          <label className="field">
            <span className="field-label">密码</span>
            <input
              className="field-input"
              type="password"
              name="password"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>

          {formError && (
            <p className="field-notice" role="alert">
              {formError}
            </p>
          )}

          <div className="actions">
            <button type="submit" className="btn btn--primary" disabled={pending}>
              {pending ? "正在创建…" : "创建账号"}
            </button>
          </div>
          <p className="hint">
            这里创建的账号都是普通账号，可以使用除账号管理外的全部功能；管理员账号只能由服务端脚本创建。
          </p>
        </form>
      </div>

      {listError && (
        <p className="detail-error mt-md" role="alert">
          {listError}
        </p>
      )}

      <div className="panel mt-lg">
        <div className="panel-head">
          <h3 className="panel-title">全部账号</h3>
        </div>

        {loading && <p className="hint">正在读取…</p>}

        {!loading && accounts.length === 0 && <p className="hint">还没有账号。</p>}

        {!loading && accounts.length > 0 && (
          <div className="table-wrap">
            <table className="table">
              <caption>共 {accounts.length} 个账号</caption>
              <thead>
                <tr>
                  <th scope="col">账号</th>
                  <th scope="col">显示名</th>
                  <th scope="col">角色</th>
                  <th scope="col">创建时间</th>
                  <th scope="col">最近登录</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((account) => (
                  <tr key={account.id}>
                    <td>{account.username}</td>
                    <td>{account.display_name}</td>
                    <td>
                      <span className="status" data-tone={roleTone(account.role)}>
                        {roleLabel(account.role)}
                      </span>
                    </td>
                    <td className="num">{formatMoment(account.created_at)}</td>
                    <td className="num">
                      {account.last_login_at === null
                        ? PLACEHOLDER
                        : formatMoment(account.last_login_at)}
                    </td>
                    <td>
                      {account.role === "admin" ? (
                        // 管理员账号不可删除：不摆一个按下必然失败的按钮
                        <span className="hint">不可删除</span>
                      ) : (
                        <button
                          type="button"
                          className="btn btn--danger btn--flat"
                          disabled={removingId === account.id}
                          onClick={() => void remove(account)}
                        >
                          {removingId === account.id ? "正在删除…" : "删除"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
