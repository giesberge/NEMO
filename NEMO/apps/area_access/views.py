from time import sleep

from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from NEMO.exceptions import InactiveUserError, NoActiveProjectsForUserError, PhysicalAccessExpiredUserError, \
	NoPhysicalAccessUserError, NoAccessiblePhysicalAccessUserError, UnavailableResourcesUserError, \
	MaximumCapacityReachedError, ReservationRequiredUserError, ScheduledOutageInProgressError
from NEMO.models import AreaAccessRecord, Door, PhysicalAccessLog, PhysicalAccessType, Project, User, UsageEvent, \
	BadgeReader
from NEMO.tasks import postpone
from NEMO.views.calendar import shorten_reservation
from NEMO.views.customization import get_customization
from NEMO.views.policy import check_policy_to_enter_this_area, check_policy_to_enter_any_area


@login_required
@permission_required('NEMO.add_areaaccessrecord')
@require_GET
def welcome_screen(request, door_id):
	door = get_object_or_404(Door, id=door_id)
	reader_id = request.GET.get("reader_id")
	badge_reader = BadgeReader.objects.get(id=reader_id) if reader_id else BadgeReader.default()
	return render(request, 'area_access/welcome_screen.html', {'area': door.area, 'door': door, 'badge_reader': badge_reader})


@login_required
@permission_required('NEMO.change_areaaccessrecord')
@require_GET
def farewell_screen(request, door_id):
	door = get_object_or_404(Door, id=door_id)
	reader_id = request.GET.get("reader_id")
	badge_reader = BadgeReader.objects.get(id=reader_id) if reader_id else BadgeReader.default()
	return render(request, 'area_access/farewell_screen.html', {'area': door.area, 'door': door, 'badge_reader': badge_reader})


@login_required
@permission_required('NEMO.add_areaaccessrecord')
@require_POST
def login_to_area(request, door_id):
	door = get_object_or_404(Door, id=door_id)

	badge_number = request.POST.get('badge_number', '')
	if badge_number == '':
		return render(request, 'area_access/badge_not_found.html')
	try:
		badge_number = int(badge_number)
		user = User.objects.get(badge_number=badge_number)
	except (User.DoesNotExist, ValueError):
		return render(request, 'area_access/badge_not_found.html')

	log = PhysicalAccessLog()
	log.user = user
	log.door = door
	log.time = timezone.now()
	log.result = PhysicalAccessType.DENY  # Assume the user does not have access

	facility_name = get_customization('facility_name')

	# Check policy for entering an area
	try:
		check_policy_to_enter_any_area(user=user)
	except InactiveUserError:
		log.details = "This user is not active, preventing them from entering any access controlled areas."
		log.save()
		return render(request, 'area_access/inactive.html')

	except NoActiveProjectsForUserError:
		log.details = "The user has no active projects, preventing them from entering an access controlled area."
		log.save()
		return render(request, 'area_access/no_active_projects.html')

	except PhysicalAccessExpiredUserError:
		log.details = "This user was blocked from this physical access level because their physical access has expired."
		log.save()
		message = f"Your physical access to the {facility_name} has expired. Have you completed your safety training within the last year? Please visit the User Office to renew your access."
		return render(request, 'area_access/physical_access_denied.html', {'message': message})

	except NoPhysicalAccessUserError:
		log.details = "This user does not belong to ANY physical access levels."
		log.save()
		message = f"You have not been granted physical access to any {facility_name} area. Please visit the User Office if you believe this is an error."
		return render(request, 'area_access/physical_access_denied.html', {'message': message})

	max_capacity_reached = False
	reservation_requirement_failed = False
	scheduled_outage_in_progress = False
	# Check policy to enter this area
	try:
		check_policy_to_enter_this_area(area=door.area, user=user)
	except NoAccessiblePhysicalAccessUserError:
		log.details = "This user is not assigned to a physical access level that allows access to this door at this time."
		log.save()
		message = f"You do not have access to this area of the {facility_name} at this time. Please visit the User Office if you believe this is an error."
		return render(request, 'area_access/physical_access_denied.html', {'message': message})

	except UnavailableResourcesUserError as error:
		log.details = "The user was blocked from entering this area because a required resource was unavailable."
		log.save()
		return render(request, 'area_access/resource_unavailable.html', {'unavailable_resources': error.resources})

	except MaximumCapacityReachedError as error:
		# deal with this error after checking if the user is already logged in
		max_capacity_reached = error

	except ScheduledOutageInProgressError as error:
		# deal with this error after checking if the user is already logged in
		scheduled_outage_in_progress = error

	except ReservationRequiredUserError:
		# deal with this error after checking if the user is already logged in
		reservation_requirement_failed = True

	current_area_access_record = user.area_access_record()
	if current_area_access_record and current_area_access_record.area == door.area:
		# No log entry necessary here because all validation checks passed.
		# The log entry is captured when the subsequent choice is made by the user.
		return render(request, 'area_access/already_logged_in.html', {
			'area': door.area,
			'project': current_area_access_record.project,
			'badge_number': user.badge_number,
			'reservation_requirement_failed': reservation_requirement_failed,
			'max_capacity_reached': max_capacity_reached,
			'scheduled_outage_in_progress': scheduled_outage_in_progress,
		})

	if scheduled_outage_in_progress:
		log.details = f"The user was blocked from entering this area because the {scheduled_outage_in_progress.area.name} has a scheduled outage in progress."
		log.save()
		message = f"The {scheduled_outage_in_progress.area.name} is inaccessible because a scheduled outage is in progress."
		return render(request, 'area_access/physical_access_denied.html', {'message': message})

	if max_capacity_reached:
		log.details = f"The user was blocked from entering this area because the {max_capacity_reached.area.name} has reached its maximum capacity of {max_capacity_reached.area.maximum_capacity} people at a time."
		log.save()
		message = f"The {max_capacity_reached.area.name} has reached its maximum capacity. Please wait for somebody to leave and try again."
		return render(request, 'area_access/physical_access_denied.html', {'message': message})

	if reservation_requirement_failed:
		log.details = f"The user was blocked from entering this area because the user does not have a current reservation for the {door.area}."
		log.save()
		message = "You do not have a current reservation for this area. Please make a reservation before trying to access this area."
		return render(request, 'area_access/physical_access_denied.html', {'message': message})

	previous_area = None
	if user.active_project_count() >= 1:
		if user.active_project_count() == 1:
			project = user.active_projects()[0]
		else:
			project_id = request.POST.get('project_id')
			if not project_id:
				# No log entry necessary here because all validation checks passed, and the user must indicate which project
				# the wish to login under. The log entry is captured when the subsequent choice is made by the user.
				return render(request, 'area_access/choose_project.html', {'area': door.area, 'user': user})
			else:
				project = get_object_or_404(Project, id=project_id)
				if project not in user.active_projects():
					log.details = "The user attempted to bill the project named {}, but they are not a member of that project.".format(
						project.name)
					log.save()
					message = "You are not authorized to bill this project."
					return render(request, 'area_access/physical_access_denied.html', {'message': message})

		log.result = PhysicalAccessType.ALLOW
		log.save()

		# Automatically log the user out of any previous area before logging them in to the new area.
		if user.in_area():
			previous_area_access_record = user.area_access_record()
			previous_area_access_record.end = timezone.now()
			previous_area_access_record.save()
			previous_area = previous_area_access_record.area

		record = AreaAccessRecord()
		record.area = door.area
		record.customer = user
		record.project = project
		record.save()
		unlock_door(door.id)
		return render(request, 'area_access/login_success.html', {'area': door.area, 'name': user.first_name, 'project': record.project, 'previous_area': previous_area})


@postpone
def unlock_door(door_id):
	door = Door.objects.get(id=door_id)
	door.interlock.unlock()
	sleep(8)
	door.interlock.lock()


@login_required
@permission_required('NEMO.change_areaaccessrecord')
@require_POST
def logout_of_area(request, door_id):
	try:
		badge_number = int(request.POST.get('badge_number', ''))
		user = User.objects.get(badge_number=badge_number)
	except (User.DoesNotExist, ValueError):
		return render(request, 'area_access/badge_not_found.html')
	record = user.area_access_record()
	# Allow the user to log out of any area, even if this is a logout tablet for a different area.
	if record:
		record.end = timezone.now()
		record.save()
		# Shorten the user's area reservation since the user is now leaving
		shorten_reservation(user, record.area)
		busy_tools = UsageEvent.objects.filter(end=None, user=user)
		if busy_tools:
			return render(request, 'area_access/logout_warning.html', {'area': record.area, 'name': user.first_name, 'tools_in_use': busy_tools})
		else:
			return render(request, 'area_access/logout_success.html', {'area': record.area, 'name': user.first_name})
	else:
		return render(request, 'area_access/not_logged_in.html')


@login_required
@permission_required('NEMO.change_areaaccessrecord')
@require_POST
def open_door(request, door_id):
	door = get_object_or_404(Door, id=door_id)
	badge_number = request.POST.get('badge_number', '')
	try:
		badge_number = int(badge_number)
		user = User.objects.get(badge_number=badge_number)
	except (User.DoesNotExist, ValueError):
		return render(request, 'area_access/badge_not_found.html')
	if user.area_access_record() and user.area_access_record().area == door.area:
		log = PhysicalAccessLog(user=user, door=door, time=timezone.now(), result=PhysicalAccessType.ALLOW, details="The user was permitted to enter this area, and already had an active area access record for this area.")
		log.save()
		unlock_door(door.id)
		return render(request, 'area_access/door_is_open.html')
	return render(request, 'area_access/not_logged_in.html', {'area': door.area})
