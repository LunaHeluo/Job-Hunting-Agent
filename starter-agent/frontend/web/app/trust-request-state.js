export function createTrustReadOwnership() {
  const generations = new Map();

  function capture({ overlay, overlayEpoch, epoch, route, apiBase, lane, selection }) {
    const generation = (generations.get(lane) || 0) + 1;
    generations.set(lane, generation);
    return Object.freeze({
      overlay,
      overlayEpoch,
      epoch,
      route,
      apiBase,
      lane,
      selection,
      generation,
    });
  }

  function isCurrent(token, { overlay, overlayEpoch, epoch, route, apiBase, lane, selection }) {
    return token.overlay === overlay
      && token.overlayEpoch === overlayEpoch
      && token.epoch === epoch
      && token.route === route
      && token.apiBase === apiBase
      && token.lane === lane
      && token.selection === selection
      && token.generation === generations.get(lane);
  }

  return Object.freeze({ capture, isCurrent });
}
