import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
import { ThemeProvider } from "./components/theme-provider"
import { AuthProvider } from "./contexts/AuthContext"
import { ProtectedRoute } from "./components/ProtectedRoute"
import { Toaster } from "./components/ui/sonner"
import Login from "./pages/Login"
import Manage from "./pages/Manage"
import TestPage from "./pages/Test"

function App() {
  return (
    <ThemeProvider defaultTheme="system" storageKey="sub2gen-ui-theme">
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<Navigate to="/login" replace />} />
            <Route path="/login" element={<Login />} />
            <Route element={<ProtectedRoute />}>
              <Route path="/manage" element={<Manage />} />
              <Route path="/test" element={<TestPage />} />
            </Route>
          </Routes>
        </BrowserRouter>
        <Toaster position="bottom-right" />
      </AuthProvider>
    </ThemeProvider>
  )
}

export default App
