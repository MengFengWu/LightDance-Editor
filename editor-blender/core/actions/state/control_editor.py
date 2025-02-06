# import traceback
from typing import cast

import bpy

from ....api.control_agent import control_agent
from ....core.models import FiberData, LEDData
from ....properties.types import LightType
from ....schemas.mutations import MutDancerStatusPayload
from ...log import logger
from ...models import EditingData, EditMode, PartType, SelectMode
from ...states import state
from ...utils.convert import control_status_state_to_mut
from ...utils.notification import notify
from ...utils.object import clear_selection
from ...utils.ui import redraw_area
from .app_state import set_requesting

# from .color_map import (
#     apply_color_map_updates_add_or_delete,
#     apply_color_map_updates_update,
# )
from .control_map import apply_control_map_updates
from .current_status import update_current_status_by_index

# from .led_map import apply_led_map_updates_add_or_delete, apply_led_map_updates_update


def attach_editing_control_frame():
    """Attach to editing frame and sync location to ld_position"""
    if not bpy.context:
        return
    current_frame = state.current_editing_frame

    state.current_editing_detached = False
    state.current_editing_frame_synced = True

    if current_frame != bpy.context.scene.frame_current:
        bpy.context.scene.frame_current = current_frame

    sync_editing_control_frame_properties()


def sync_editing_control_frame_properties():
    """Sync location to ld_position"""
    show_dancer_dict = dict(zip(state.dancer_names, state.show_dancers))
    for dancer in state.dancers_array:
        if not show_dancer_dict[dancer.name]:
            continue
        dancer_obj: bpy.types.Object | None = bpy.data.objects.get(dancer.name)
        if dancer_obj is not None:
            part_objs: list[bpy.types.Object] = getattr(dancer_obj, "children")
            part_obj_names: list[str] = [
                getattr(obj, "ld_part_name") for obj in part_objs
            ]

            for part in dancer.parts:
                if part.name not in part_obj_names:
                    continue

                part_index = part_obj_names.index(part.name)
                part_obj = part_objs[part_index]
                part_type = getattr(part_obj, "ld_light_type")

                # Re-trigger update
                if part_type == LightType.FIBER.value:
                    ld_color: int = getattr(part_obj, "ld_color")
                    setattr(part_obj, "ld_color", ld_color)
                    ld_alpha: int = getattr(part_obj, "ld_alpha")
                    setattr(part_obj, "ld_alpha", ld_alpha)

                elif part_type == LightType.LED.value:
                    ld_effect: int = getattr(part_obj, "ld_effect")
                    setattr(part_obj, "ld_effect", ld_effect)
                    ld_alpha: int = getattr(part_obj, "ld_alpha")
                    setattr(part_obj, "ld_alpha", ld_alpha)


async def add_control_frame():
    if not bpy.context:
        return
    start = bpy.context.scene.frame_current
    controlData = control_status_state_to_mut(state.current_status)

    set_requesting(True)
    try:
        await control_agent.add_frame(start, False, controlData)
        notify("INFO", "Added control frame")
    except Exception:
        logger.exception("Failed to add control frame")
        notify("WARNING", "Cannot add control frame")

    set_requesting(False)


# TODO finish this function
async def save_control_frame(start: int | None = None):
    if not bpy.context:
        return
    id = state.editing_data.frame_id

    fade: bool = getattr(bpy.context.window_manager, "ld_fade")

    controlData: list[MutDancerStatusPayload] = []
    default_color = list(state.color_map.keys())[0]

    show_dancer_dict = dict(zip(state.dancer_names, state.show_dancers))

    for dancer in state.dancers_array:
        partControlData: MutDancerStatusPayload = []

        if not show_dancer_dict[dancer.name]:
            ctrl_part_dict = state.control_map[id].status[dancer.name]
            for part in dancer.parts:
                if part.name not in ctrl_part_dict.keys():
                    if part.type == PartType.FIBER:
                        partControlData.append((default_color, 0))
                    elif part.type == PartType.LED:
                        partControlData.append((-1, 0))
                    continue

                if part.type == PartType.FIBER:
                    part_data = cast(FiberData, ctrl_part_dict[part.name])
                    color_id = part_data.color_id
                    ld_alpha = part_data.alpha
                    partControlData.append((color_id, ld_alpha))
                elif part.type == PartType.LED:
                    part_data = cast(LEDData, ctrl_part_dict[part.name])
                    effect_id = part_data.effect_id
                    ld_alpha = part_data.alpha
                    partControlData.append((effect_id, ld_alpha))

            controlData.append(partControlData)
            continue

        obj: bpy.types.Object | None = bpy.data.objects.get(dancer.name)
        if obj is not None:
            part_objs: list[bpy.types.Object] = getattr(obj, "children")
            part_obj_names: list[str] = [
                getattr(obj, "ld_part_name") for obj in part_objs
            ]

            for part in dancer.parts:
                if part.name not in part_obj_names:
                    if part.type == PartType.FIBER:
                        partControlData.append((default_color, 0))
                    elif part.type == PartType.LED:
                        partControlData.append((-1, 0))
                    continue

                part_index = part_obj_names.index(part.name)
                part_obj = part_objs[part_index]

                if part.type == PartType.FIBER:
                    color_id = part_obj["ld_color"]
                    ld_alpha: int = getattr(part_obj, "ld_alpha")
                    partControlData.append((color_id, ld_alpha))
                elif part.type == PartType.LED:
                    effect_id = part_obj["ld_effect"]
                    ld_alpha: int = getattr(part_obj, "ld_alpha")
                    partControlData.append((effect_id, ld_alpha))

        else:
            for part in dancer.parts:
                if part.type == PartType.FIBER:
                    partControlData.append((default_color, 0))
                elif part.type == PartType.LED:
                    partControlData.append((-1, 0))

        controlData.append(partControlData)

    set_requesting(True)
    try:
        await control_agent.save_frame(id, controlData, fade=fade, start=start)
        notify("INFO", "Saved control frame")

        # Cancel editing
        ok = await control_agent.cancel_edit(id)

        if ok is not None and ok:
            # Reset editing state
            state.current_editing_frame = -1
            state.current_editing_detached = False
            state.current_editing_frame_synced = False
            state.edit_state = EditMode.IDLE

            # Imediately apply changes produced by editing
            apply_control_map_updates()

            redraw_area({"VIEW_3D", "DOPESHEET_EDITOR"})
        else:
            notify("WARNING", "Cannot exit editing")
    except Exception:
        logger.exception("Failed to save control frame")
        notify("WARNING", "Cannot save control frame")

    set_requesting(False)


async def delete_control_frame():
    index = state.current_control_index
    id = state.control_record[index]

    set_requesting(True)
    try:
        await control_agent.delete_frame(id)
        notify("INFO", f"Deleted control frame: {id}")
    except Exception:
        logger.exception("Failed to delete control frame")
        notify("WARNING", "Cannot delete control frame")

    set_requesting(False)


async def request_edit_control() -> bool:
    # if state.color_map_pending.add_or_delete:
    #     apply_color_map_updates_add_or_delete()
    # if state.color_map_pending.update:
    #     apply_color_map_updates_update()
    # if state.led_map_pending.add_or_delete:
    #     apply_led_map_updates_add_or_delete()
    # if state.led_map_pending.update:
    #     apply_led_map_updates_update()
    # if state.control_map_pending:
    #     apply_control_map_updates()

    index = state.current_control_index
    control_id = state.control_record[index]
    control_frame = state.control_map[control_id]

    set_requesting(True)
    ok = await control_agent.request_edit(control_id)
    set_requesting(False)

    if ok is not None and ok:
        # Init editing state
        state.current_editing_frame = control_frame.start
        state.editing_data = EditingData(
            start=state.current_editing_frame, frame_id=control_id, index=index
        )
        state.edit_state = EditMode.EDITING

        attach_editing_control_frame()
        update_current_status_by_index()

        redraw_area({"VIEW_3D", "DOPESHEET_EDITOR"})
        return True
    else:
        notify("WARNING", "Edit request rejected")
        return False


async def cancel_edit_control():
    index = state.current_control_index
    id = state.control_record[index]

    set_requesting(True)
    try:
        ok = await control_agent.cancel_edit(id)

        if ok is not None and ok:
            # Revert modification
            update_current_status_by_index()

            # Reset editing state
            state.current_editing_frame = -1
            state.current_editing_detached = False
            state.current_editing_frame_synced = False
            state.edit_state = EditMode.IDLE

            redraw_area({"VIEW_3D", "DOPESHEET_EDITOR"})
        else:
            notify("WARNING", "Cannot cancel edit")

    except Exception:
        logger.exception("Failed to cancel edit control frame")
        notify("WARNING", "Cannot cancel edit")

    set_requesting(False)


def toggle_dancer_mode():
    bpy.context.view_layer.objects.active = None  # type: ignore
    state.selected_obj_type = None
    clear_selection()
    state.selection_mode = SelectMode.DANCER_MODE
    redraw_area({"VIEW_3D", "DOPESHEET_EDITOR"})


def toggle_part_mode():
    bpy.context.view_layer.objects.active = None  # type: ignore
    state.selected_obj_type = None
    clear_selection()
    state.selection_mode = SelectMode.PART_MODE
    redraw_area({"VIEW_3D", "DOPESHEET_EDITOR"})
