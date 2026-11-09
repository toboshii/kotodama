-- Optional seed table: model id -> creature archetype key.
--
-- Non-player NPCs (race 0 / "precomposed") carry a model id instead of a
-- player race. Look yours up with:  //koto inspect t   (prints models=...)
-- then map it here, e.g.
--     [1234] = 'moogle',
-- Valid keys are the creature entries in server/archetypes.json
-- (moogle, goblin, yagudo, quadav, orc, tonberry, antica, sahagin,
--  mamool, lamia, ...). Unmapped models fall back to the sidecar's
-- name-keyword matching and your per-NPC overrides, so this file may stay
-- empty; it only saves you from writing overrides one NPC at a time.

return {
}
