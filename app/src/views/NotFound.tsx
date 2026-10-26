import { Link, useLocation } from 'react-router';

export default function NotFound() {
  const { pathname } = useLocation();

  return (
    <div>
      <h1>Page not found</h1>
      <p>No page exists at <code>{pathname}</code>.</p>
      <Link to="/dashboard">Go to dashboard</Link>
    </div>
  );
}
