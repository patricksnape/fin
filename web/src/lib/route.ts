import { useEffect, useState } from "react";

export interface Route { page: string; params: URLSearchParams }

function read(): Route {
  const [page, qs = ""] = location.hash.slice(1).split("?");
  return { page, params: new URLSearchParams(qs) };
}

/** Hash routes like #accounts?account=Assets:UBS:Checking, so a link can carry a selection. */
export function useRoute(): Route {
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export function go(page: string, params?: Record<string, string>) {
  location.hash = params ? `${page}?${new URLSearchParams(params)}` : page;
}
