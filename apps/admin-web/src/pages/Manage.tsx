import { useMemo, useEffect, useCallback } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { Layout } from "../components/Layout"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "../components/ui/tabs"
import { TokenManagement } from "../features/admin/TokenManagement"
import { ControlPlane } from "../features/admin/ControlPlane"
import { SystemSettings } from "../features/admin/SystemSettings"
import { RequestLogs } from "../features/admin/RequestLogs"
import { CacheManagement } from "../features/admin/CacheManagement"
import { AIGateway } from "../features/admin/AIGateway"
import { ApiKeyManagement } from "../features/admin/ApiKeyManagement"
import { AdobeSettings } from "../features/admin/AdobeSettings"
import { RunwaySettings } from "../features/admin/RunwaySettings"
import { GeminiGenSettings } from "../features/admin/GeminiGenSettings"
import { cn } from "@/lib/utils"
import { MANAGE_TABS, parseManageTab, type ManageTab } from "../features/admin/manageTabs"

export default function Manage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = useMemo(
    () => parseManageTab(searchParams.get("tab")),
    [searchParams]
  )
  const setTab = useCallback((v: string) => {
    if (v === "platform") setSearchParams({})
    else setSearchParams({ tab: v })
  }, [setSearchParams])
  useEffect(() => {
    const raw = searchParams.get("tab")
    if (raw && !MANAGE_TABS.includes(raw as ManageTab)) {
      setSearchParams({}, { replace: true })
    }
  }, [searchParams, setSearchParams])

  return (
    <Layout>
      <Tabs value={tab} onValueChange={setTab} className="w-full">
        <div className="border-b border-border mb-6 flex flex-wrap items-end gap-6">
          <TabsList className="flex h-auto w-full min-w-0 flex-1 flex-wrap justify-start rounded-none bg-transparent p-0 gap-x-6 gap-y-0">
            <TabsTrigger
              value="platform"
              className={cn(
                "rounded-none border-b-2 border-transparent px-1 py-3 data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
              )}
            >
              Platform
            </TabsTrigger>
            <TabsTrigger
              value="tokens"
              className={cn(
                "rounded-none border-b-2 border-transparent px-1 py-3 data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
              )}
            >
              Flow accounts
            </TabsTrigger>
            <TabsTrigger
              value="apikeys"
              className={cn(
                "rounded-none border-b-2 border-transparent px-1 py-3 data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
              )}
            >
              API key manager
            </TabsTrigger>
            <TabsTrigger
              value="settings"
              className={cn(
                "rounded-none border-b-2 border-transparent px-1 py-3 data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
              )}
            >
              System settings
            </TabsTrigger>
            <TabsTrigger
              value="logs"
              className={cn(
                "rounded-none border-b-2 border-transparent px-1 py-3 data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
              )}
            >
              Request logs
            </TabsTrigger>
            <TabsTrigger
              value="adobe"
              className={cn(
                "rounded-none border-b-2 border-transparent px-1 py-3 data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
              )}
            >
              Adobe
            </TabsTrigger>
            <TabsTrigger
              value="runway"
              className={cn(
                "rounded-none border-b-2 border-transparent px-1 py-3 data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
              )}
            >
              Runway
            </TabsTrigger>
            <TabsTrigger
              value="gateway"
              className={cn(
                "rounded-none border-b-2 border-transparent px-1 py-3 data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
              )}
            >
              AI Gateway
            </TabsTrigger>
            <TabsTrigger
              value="geminigen"
              className={cn(
                "rounded-none border-b-2 border-transparent px-1 py-3 data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
              )}
            >
              GeminiGen
            </TabsTrigger>
            <TabsTrigger
              value="cache"
              className={cn(
                "rounded-none border-b-2 border-transparent px-1 py-3 data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none"
              )}
            >
              Cache management
            </TabsTrigger>
          </TabsList>
          <Link
            to="/test"
            className={cn(
              "text-sm font-medium py-3 px-1 border-b-2 border-transparent text-muted-foreground hover:text-foreground transition-colors shrink-0 mb-px"
            )}
          >
            Test page
          </Link>
        </div>

        <TabsContent value="platform" className="mt-0 outline-hidden focus-visible:ring-0">
          <div className="animate-in fade-in duration-300">
            <ControlPlane />
          </div>
        </TabsContent>

        <TabsContent value="tokens" className="mt-0 outline-hidden focus-visible:ring-0">
          <div className="animate-in fade-in duration-300">
            <TokenManagement />
          </div>
        </TabsContent>
        <TabsContent value="settings" className="mt-0 outline-hidden focus-visible:ring-0">
          <div className="animate-in fade-in duration-300">
            <SystemSettings active={true} />
          </div>
        </TabsContent>
        <TabsContent value="apikeys" className="mt-0 outline-hidden focus-visible:ring-0">
          <div className="animate-in fade-in duration-300">
            <ApiKeyManagement />
          </div>
        </TabsContent>
        <TabsContent value="logs" className="mt-0 outline-hidden focus-visible:ring-0">
          <div className="animate-in fade-in duration-300">
            <RequestLogs />
          </div>
        </TabsContent>
        <TabsContent value="adobe" className="mt-0 outline-hidden focus-visible:ring-0">
          <div className="animate-in fade-in duration-300">
            <AdobeSettings active={tab === "adobe"} />
          </div>
        </TabsContent>
        <TabsContent value="runway" className="mt-0 outline-hidden focus-visible:ring-0">
          <div className="animate-in fade-in duration-300">
            <RunwaySettings active={tab === "runway"} />
          </div>
        </TabsContent>
        <TabsContent value="gateway" className="mt-0 outline-hidden focus-visible:ring-0">
          <div className="animate-in fade-in duration-300">
            <AIGateway active={tab === "gateway"} />
          </div>
        </TabsContent>
        <TabsContent value="geminigen" className="mt-0 outline-hidden focus-visible:ring-0">
          <div className="animate-in fade-in duration-300">
            <GeminiGenSettings active={tab === "geminigen"} />
          </div>
        </TabsContent>
        <TabsContent value="cache" className="mt-0 outline-hidden focus-visible:ring-0">
          <div className="animate-in fade-in duration-300">
            <CacheManagement active={true} />
          </div>
        </TabsContent>
      </Tabs>
    </Layout>
  )
}
