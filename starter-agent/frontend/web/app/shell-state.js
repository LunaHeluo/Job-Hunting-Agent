export const PRIMARY_ROUTES = Object.freeze(["workbench", "version-map", "applications"]);
export const ADVANCED_VIEWS = Object.freeze(["knowledge", "capabilities", "trust"]);

export function resolveShellRoute(hash, requested = "") {
  const byHash = {
    "#/workbench": "workbench",
    "#/version-map": "version-map",
    "#/applications": "applications",
  };
  if (byHash[hash]) return byHash[hash];
  if (PRIMARY_ROUTES.includes(requested)) return requested;
  return "workbench";
}

export function createShellState(initialRoute = "workbench") {
  let primaryRoute = PRIMARY_ROUTES.includes(initialRoute) ? initialRoute : "workbench";
  let overlay = null;
  let overlayEpoch = 0;
  const scrollPositions = new Map();

  return Object.freeze({
    snapshot: () => Object.freeze({ primaryRoute, overlay, overlayEpoch }),
    navigate(route) {
      if (!PRIMARY_ROUTES.includes(route)) throw new Error(`Unknown primary route: ${route}`);
      primaryRoute = route;
      return this.snapshot();
    },
    openOverlay(type) {
      if (!ADVANCED_VIEWS.includes(type)) throw new Error(`Unknown overlay: ${type}`);
      overlay = type;
      overlayEpoch += 1;
      return this.snapshot();
    },
    closeOverlay() {
      overlay = null;
      overlayEpoch += 1;
      return this.snapshot();
    },
    captureOverlayRequest: () => Object.freeze({ overlay, overlayEpoch }),
    isOverlayRequestCurrent: token => token.overlay === overlay && token.overlayEpoch === overlayEpoch,
    rememberScroll(key, top) { scrollPositions.set(key, Math.max(0, Number(top) || 0)); },
    scrollFor: key => scrollPositions.get(key) || 0,
  });
}
