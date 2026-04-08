import { useState } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Layout from './components/Layout';
import Dashboard from './views/Dashboard';
import Ingest from './views/Ingest';
import Review from './views/Review';
import Search from './views/Search';
import DocumentDetail from './views/DocumentDetail';
import GraphExplorer from './views/GraphExplorer';
import { defaultVaultId } from './mock/data';

export default function App() {
  const [activeVault, setActiveVault] = useState(defaultVaultId);

  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout activeVault={activeVault} onVaultChange={setActiveVault} />}>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="ingest" element={<Ingest />} />
          <Route path="review" element={<Review />} />
          <Route path="search" element={<Search />} />
          <Route path="documents/:id" element={<DocumentDetail />} />
          <Route path="documents/:id/graph" element={<GraphExplorer />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
