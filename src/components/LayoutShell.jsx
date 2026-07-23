import "./LayoutShell.css";

const NAV_ITEMS = [
  { key: "pre-specifications", label: "사전규격", href: "#pre-specifications" },
  { key: "bid-notices", label: "입찰공고", href: "#bid-notices" },
  { key: "opening-results", label: "개찰결과", href: "#opening-results" },
];

function LayoutShell({ session, activePage, onLogout, children }) {
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
            <nav className="layout-shell-nav" aria-label="업무 페이지">
              {NAV_ITEMS.map((item) => (
                <a
                  key={item.key}
                  className={activePage === item.key ? "is-active" : undefined}
                  href={item.href}
                  aria-current={activePage === item.key ? "page" : undefined}
                >
                  {item.label}
                </a>
              ))}
            </nav>
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
