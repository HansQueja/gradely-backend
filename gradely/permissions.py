from rest_framework import permissions

class isSchoolAdmin(permissions.BasePermission):
    """
    Custom permission to only allow users with role='ADMIN'.
    """
    def has_permission(self, request, view):
        # 1. User must be logged in
        if not request.user or not request.user.is_authenticated:
            return False
        
        # 2. User must have the 'ADMIN' role (and technically be approved)
        return request.user.role == 'ADMIN' and request.user.is_approved