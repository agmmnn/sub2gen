import { Link } from "react-router-dom"
import { useTheme } from "./theme-provider"
import { useAuth } from "../contexts/AuthContext"
import { Button } from "./ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "./ui/dropdown-menu"
import { Moon, Sun, Monitor, LogOut } from "lucide-react"

export function Layout({ children }: { children: React.ReactNode }) {
  const { setTheme } = useTheme()
  const { logout } = useAuth()

  return (
    <div className="min-h-screen bg-background font-sans antialiased text-foreground">
      <header className="sticky top-0 z-50 w-full border-b border-border/40 bg-background/95 backdrop-blur-sm">
        <div className="mx-auto flex h-14 max-w-7xl items-center px-6">
          <div className="mr-4 flex items-baseline gap-3">
            <Link to="/manage" className="font-bold text-xl hover:text-primary transition-colors">
              sub2gen
            </Link>
          </div>
          <div className="flex flex-1 items-center justify-end gap-2">
            
            <a href="https://github.com/agmmnn/sub2gen" target="_blank" rel="noreferrer">
              <Button variant="ghost" size="icon" className="h-8 w-8" title="GitHub Repository">
                <span className="font-bold">GH</span>
              </Button>
            </a>

            <DropdownMenu>
              <DropdownMenuTrigger render={<Button variant="ghost" size="icon" className="h-8 w-8" title="Toggle Theme" />}>
                <Sun className="h-[1.2rem] w-[1.2rem] rotate-0 scale-100 transition-all dark:-rotate-90 dark:scale-0" />
                <Moon className="absolute h-[1.2rem] w-[1.2rem] rotate-90 scale-0 transition-all dark:rotate-0 dark:scale-100" />
                <span className="sr-only">Toggle theme</span>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={() => setTheme("light")}><Sun className="mr-2 h-4 w-4"/> Light</DropdownMenuItem>
                <DropdownMenuItem onClick={() => setTheme("dark")}><Moon className="mr-2 h-4 w-4"/> Dark</DropdownMenuItem>
                <DropdownMenuItem onClick={() => setTheme("system")}><Monitor className="mr-2 h-4 w-4"/> System</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>

            <Button variant="ghost" size="sm" onClick={() => void logout()} className="h-8 gap-1">
              <LogOut className="h-3.5 w-3.5" />
              Logout
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-6 animate-in fade-in duration-500">
        {children}
      </main>
      <footer className="mt-12 pt-6 pb-6 border-t border-border text-center text-xs text-muted-foreground">
        <p>
          © 2026{" "}
          <a href="https://linux.do/u/thesmallhancat/summary" target="_blank" rel="noreferrer" className="hover:underline text-foreground">
            TheSmallHanCat
          </a>{" "}
          and{" "}
          <a href="https://linux.do/u/tibbar/summary" target="_blank" rel="noreferrer" className="hover:underline text-foreground">
            Tibbar
          </a>
          . All rights reserved.
        </p>
      </footer>
    </div>
  )
}
