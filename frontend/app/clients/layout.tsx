import { ClientCrmSidebar } from "@/components/client-crm-sidebar";

export default function ClientCrmLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-[calc(100vh-3.5rem)]">
      <ClientCrmSidebar />
      <div className="min-w-0 flex-1 overflow-x-hidden">{children}</div>
    </div>
  );
}
