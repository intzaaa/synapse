# Copyright 2021 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
from typing import TYPE_CHECKING, Dict, List, Tuple

from synapse.api.constants import EventTypes, JoinRules, Membership
from synapse.api.errors import SynapseError
from synapse.http.servlet import ResolveRoomIdMixin, RestServlet
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import admin_patterns, assert_user_is_admin
from synapse.storage.state import StateFilter
from synapse.types import JsonDict, UserID, create_requester

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class RemoveHierarchyMemberRestServlet(ResolveRoomIdMixin, RestServlet):
    """
    Puppets a local user to remove them from all rooms in a space.
    """

    PATTERNS = admin_patterns(
        "/rooms/(?P<space_id>[^/]+)/hierarchy/members/(?P<user_id>[^/]+)$"
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self._hs = hs
        self._auth = hs.get_auth()
        self._store = hs.get_datastore()
        self._room_member_handler = hs.get_room_member_handler()
        self._room_hierarchy_handler = hs.get_room_hierarchy_handler()

    async def on_DELETE(
        self, request: SynapseRequest, space_id: str, user_id: str
    ) -> Tuple[int, JsonDict]:
        """Forces a local user to leave all non-public rooms in a space.

        The space itself is always left, regardless of whether it is public.

        May succeed partially if the user fails to leave some rooms.

        Returns:
            A tuple containing the HTTP status code and a JSON dictionary containing:
             * `left`: A list of rooms that the user has been made to leave.
             * `failed`: A with entries for rooms that could not be fully processed.
                The values of the dictionary are lists of failure reasons.
                Rooms may appear here if:
                 * The user failed to leave them for any reason.
                 * The room is a space that the local homeserver is not in, and so its
                   full list of child rooms could not be determined.
                 * The room is inaccessible to the local homeserver, and it is not known
                   whether the room is a subspace containing further rooms.
                 * Some combination of the above.
        """
        requester = await self._auth.get_user_by_req(request)
        await assert_user_is_admin(self._auth, requester.user)

        space_id, _ = await self.resolve_room_id(space_id)

        target_user = UserID.from_string(user_id)

        if not self._hs.is_mine(target_user):
            raise SynapseError(400, "This endpoint can only be used with local users")

        # Fetch the list of rooms the target user is currently in
        user_rooms = await self._store.get_rooms_for_local_user_where_membership_is(
            user_id, [Membership.INVITE, Membership.JOIN, Membership.KNOCK]
        )
        user_room_ids = {room.room_id for room in user_rooms}

        # Fetch the list of rooms in the space hierarchy
        (
            descendants,
            inaccessible_room_ids,
        ) = await self._room_hierarchy_handler.get_room_descendants(space_id)
        space_room_ids = {space_id}
        space_room_ids.update(room_id for room_id, _ in descendants)

        # Determine which rooms to leave by checking join rules.
        rooms_to_check = space_room_ids.intersection(user_room_ids)
        rooms_to_leave = {space_id}  # Always leave the space, even if it is public
        state_filter = StateFilter.from_types([(EventTypes.JoinRules, "")])
        for room_id in rooms_to_check:
            current_state_ids = await self._store.get_filtered_current_state_ids(
                room_id, state_filter
            )
            join_rules_event_id = current_state_ids.get((EventTypes.JoinRules, ""))
            if join_rules_event_id is not None:
                join_rules_event = await self._store.get_event(join_rules_event_id)
                join_rules = join_rules_event.content.get("join_rule")
            else:
                # The user is invited to or has knocked on a room that is not known
                # locally. Assume that such rooms are not public and should be left.
                # If it turns out that the room is actually public, then we've not
                # actually prevented the user from joining it.
                join_rules = None
            if join_rules != JoinRules.PUBLIC:
                rooms_to_leave.add(room_id)

        # Now start leaving rooms
        failures: Dict[str, List[str]] = {
            room_id: ["Could not fully explore space or room."]
            for room_id in inaccessible_room_ids
        }
        left_rooms: List[str] = []

        fake_requester = create_requester(
            target_user, authenticated_entity=requester.user.to_string()
        )

        for room_id in rooms_to_leave:
            # There is a race condition here where the user may have left or been kicked
            # from a room since their list of memberships was fetched.
            # `update_membership` will raise if the user is no longer in the room,
            # but it's tricky to distinguish from other failure modes.

            try:
                await self._room_member_handler.update_membership(
                    requester=fake_requester,
                    target=target_user,
                    room_id=room_id,
                    action=Membership.LEAVE,
                    content={},
                    ratelimit=False,
                    require_consent=False,
                )
                left_rooms.append(room_id)
            except Exception as e:
                failures.get(room_id, []).append(str(e))

        return 200, {"left": left_rooms, "failed": failures}
