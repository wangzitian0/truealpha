import { handleCardExportGet } from "@/server/cards/xhs-card";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  return handleCardExportGet(request);
}
