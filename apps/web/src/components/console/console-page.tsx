import { Construction } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";

export function ConsolePageHeader({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="mb-8 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
        {description ? (
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            {description}
          </p>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

/** Clearly-labelled placeholder block for not-yet-implemented console surfaces. */
export function PlaceholderNotice({
  title = "Placeholder surface",
  children,
}: {
  title?: string;
  children?: React.ReactNode;
}) {
  return (
    <Card className="border-dashed">
      <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
        <span className="flex h-11 w-11 items-center justify-center rounded-full bg-surface-2 text-muted-foreground">
          <Construction className="h-5 w-5" />
        </span>
        <div className="flex items-center gap-2">
          <h3 className="font-semibold">{title}</h3>
          <Badge variant="warning">Demo</Badge>
        </div>
        <p className="max-w-md text-sm text-muted-foreground">
          {children ??
            "This area shows real layout with clearly-labelled placeholder data. Live functionality lands as the backend services come online."}
        </p>
      </CardContent>
    </Card>
  );
}
