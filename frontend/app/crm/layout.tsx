import { CrmSidebar } from "@/components/crm-sidebar";

export default function CrmLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-[calc(100vh-3.5rem)]">
      <CrmSidebar />
      <div className="min-w-0 flex-1 overflow-x-hidden">{children}</div>
    </div>
  );
}
