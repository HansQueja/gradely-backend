from django.contrib import admin
from .models import User, Student, Subject, Classroom, Quiz, QuizResult

# User Admin
admin.site.register(User)

# Classroom Admin (Helps you see who teaches what)
@admin.register(Classroom)
class ClassroomAdmin(admin.ModelAdmin):
    list_display = ('section_name', 'subject', 'teacher', 'school_year')
    list_filter = ('school_year', 'subject')

# Quiz Admin (Shows the new stats)
@admin.register(Quiz)
class QuizAdmin(admin.ModelAdmin):
    list_display = ('title', 'classroom', 'mean_score', 'attendees_count', 'created_at')
    readonly_fields = ('mean_score', 'min_score', 'max_score', 'attendees_count') # Protect stats from manual edits

# Others
admin.site.register(Student)
admin.site.register(Subject)
admin.site.register(QuizResult)