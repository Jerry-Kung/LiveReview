/**
 * 页眉：项目名称与用户区。
 *
 * 用户登录是预留功能区，只承载界面状态，后端尚未提供账号体系，
 * 因此界面明确标注「未接入」，不做假的登录成功反馈。
 */

export type SessionUser = { name: string; role: string } | null;

export default function Header({
  user,
  onSignIn,
  onSignOut,
}: {
  user: SessionUser;
  onSignIn: () => void;
  onSignOut: () => void;
}) {
  return (
    <header className="masthead">
      <div className="masthead-brand">
        <span className="mark" aria-hidden="true" />
        <h1 className="masthead-name">直播视频复盘分析工作台</h1>
        <span className="masthead-scope">营销直播录屏理解与复盘</span>
      </div>

      <div className="masthead-side">
        {user ? (
          <div className="account">
            <span className="avatar" aria-hidden="true">
              {user.name.slice(0, 1)}
            </span>
            <span className="account-text">
              <strong>{user.name}</strong>
              <span>{user.role}</span>
            </span>
            <button type="button" className="btn btn--quiet" onClick={onSignOut}>
              退出
            </button>
          </div>
        ) : (
          <button type="button" className="btn btn--quiet" onClick={onSignIn}>
            用户登录
          </button>
        )}
      </div>
    </header>
  );
}
