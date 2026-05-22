import { redirect } from "next/navigation";

/** Root → Brief (the default landing surface, UX §1.1). RequireAuth on the
 *  app shell bounces unauthenticated callers on to /login. */
export default function RootPage() {
  redirect("/brief");
}
