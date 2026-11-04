/** The client's copy of the role table — for what to *offer*, never for what to *allow*.

This mirrors `app/security/rbac.py:_ROLE_PERMISSIONS` so the console does not put a Vote button in
front of a role the room will refuse. It is deliberately a display gate and nothing more: §10.1
says permission checks belong on the server, and every control here re-issues a frame or a request
the server validates from the JWT it signed, not from this file. If the two ever disagree, the
server is right and the console is showing a button that gets refused.

Ownership is the one thing a role cannot express. §7.2.1 keeps `host_user_id` per lounge precisely
because a global "host" role is not what governs a single room, so ending a session checks the id
rather than the role — and a transferred Host gains that without gaining the role.
*/

import type { Role } from '../api/rest';

export type Capability =
  | 'vote'
  | 'chat'
  | 'submitBattle'
  | 'voteBattle'
  | 'openBattle'
  | 'resolveBattle'
  | 'approveSegue'
  | 'createLounge'
  | 'moderate'
  | 'manageAcoustic';

const BY_ROLE: Record<Role, readonly Capability[]> = {
  listener: ['vote', 'chat', 'submitBattle', 'voteBattle'],
  co_dj: [
    'vote', 'chat', 'submitBattle', 'voteBattle',
    'openBattle', 'resolveBattle', 'approveSegue',
  ],
  host: [
    'vote', 'chat', 'submitBattle', 'voteBattle',
    'openBattle', 'resolveBattle', 'approveSegue',
    'createLounge', 'moderate',
  ],
  admin: [
    'vote', 'chat', 'submitBattle', 'voteBattle',
    'openBattle', 'resolveBattle', 'approveSegue',
    'createLounge', 'moderate', 'manageAcoustic',
  ],
};

export function can(role: Role | null | undefined, capability: Capability): boolean {
  if (!role) return false;
  const held = BY_ROLE[role];
  return held !== undefined && held.includes(capability);
}

/** Who may close this particular lounge: its owner, or an Admin as §10.1's operator tier. */
export function canEndSession(
  role: Role | null | undefined,
  userId: string | null,
  hostUserId: string | null,
): boolean {
  if (role === 'admin') return true;
  return userId !== null && hostUserId !== null && userId === hostUserId;
}

/** Emojis the room scores, so a picker cannot offer a reaction §6.2's sentiment path refuses.

Keyed off `backend/app/gateway/chat.py:_EMOJI_SCORE`. A reaction the server does not know is
refused rather than averaged, so an unre scored button is a guaranteed error frame on every press.
*/
export const REACTION_EMOJIS = [
  '🔥', '🙌', '🎉', '❤', '👌', '✨', '😍', '😎', '💯', '🎧', '👀',
  '😴', '🥱', '👎', '😬', '😩', '💀', '🙄', '😑', '😕',
] as const;

/** §5.1's message cap, mirrored so the composer says "too long" before the socket does. */
export const MAX_MESSAGE_CHARS = 500;
