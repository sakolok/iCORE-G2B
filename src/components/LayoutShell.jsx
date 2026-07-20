import "./LayoutShell.css";

function LayoutShell({ session, onLogout, children }) {
  const email = session?.email || session?.username || "iCore 사용자";
  const avatarLabel = email.slice(0, 1).toUpperCase();

  return (
    <div className="layout-shell">
      <header className="layout-shell-header">
        <div className="layout-shell-header-inner">
          <div className="layout-shell-brand-group">
            <div className="layout-shell-brand" aria-label="iCore">
              <span className="layout-shell-brand-mark" aria-hidden="true">i</span>
              <strong>iCore</strong>
            </div>
            <span className="layout-shell-divider" aria-hidden="true" />
            <span className="layout-shell-current">개찰결과</span>
          </div>

          <div className="layout-shell-user">
            <span className="layout-shell-avatar" aria-hidden="true">{avatarLabel}</span>
            <span className="layout-shell-user-copy">
              <strong>{email}</strong>
              <span>개인 검토함</span>
            </span>
            {onLogout ? (
              <button className="layout-shell-logout" type="button" onClick={onLogout}>
                로그아웃
              </button>
            ) : null}
          </div>
        </div>
      </header>
      <main className="layout-shell-content">{children}</main>
    </div>
  );
}

export default LayoutShell;
