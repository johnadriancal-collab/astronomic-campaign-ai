"use client"

import { Tabs as TabsPrimitive } from "@base-ui/react/tabs"

import { cn } from "@/lib/utils"

function Tabs({ ...props }: TabsPrimitive.Root.Props) {
  return <TabsPrimitive.Root data-slot="tabs" {...props} />
}

function TabsList({ className, ...props }: TabsPrimitive.List.Props) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      className={cn("flex items-center gap-4 border-b border-border", className)}
      {...props}
    />
  )
}

function TabsTab({ className, ...props }: TabsPrimitive.Tab.Props) {
  return (
    <TabsPrimitive.Tab
      data-slot="tabs-tab"
      className={cn(
        "border-b-2 border-transparent px-1 pb-3 text-sm text-muted-foreground transition-colors hover:text-foreground",
        "data-[active]:border-primary data-[active]:font-medium data-[active]:text-foreground",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        className
      )}
      {...props}
    />
  )
}

function TabsPanel({ className, ...props }: TabsPrimitive.Panel.Props) {
  return <TabsPrimitive.Panel data-slot="tabs-panel" className={cn("pt-6", className)} {...props} />
}

export { Tabs, TabsList, TabsTab, TabsPanel }
