-- Optional seed table: model id -> archetype key.
--
-- Non-player NPCs (race 0 / "precomposed") carry a model id instead of a
-- player race. Look yours up with:  //koto inspect t   (prints models=...)
-- then map it here, e.g.
--     [1234] = 'moogle',
-- Valid keys are the archetype entries in server/archetypes.json: creatures
-- (moogle, goblin, yagudo, quadav, orc, tonberry, antica, sahagin, mamool,
-- lamia, ...) and also races, for fixed-model townsfolk (hume_m, taru_f,
-- mithra_child, child_f, ...). Unmapped models fall back to the sidecar's
-- name-keyword matching, voice casting from the NPC's lines, and your
-- per-NPC overrides. So this file may stay short. It only saves you from
-- writing overrides one NPC at a time.

return {
    [82] = 'moogle',        -- Moogle, Repairer Moogle, Porter Moogle
    [182] = 'mithra_child', -- Ahkk Jharcham (Aht Urhgan Whitegate)
}
